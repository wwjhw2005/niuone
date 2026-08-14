"""Optional SOCKS5H transport for mainland-China market-data sources.

The standard library does not natively understand SOCKS proxy URLs.  Keep the
implementation here small and explicit so existing ``urllib`` callers retain
their bounded timeouts without adding a process-global socket monkeypatch.
"""
from __future__ import annotations

import http.client
import os
import socket
import ssl
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import SplitResult, urlsplit


PROXY_ENV_NAME = "DASHBOARD_CN_DATA_PROXY_URL"
SUPPORTED_PROXY_SCHEME = "socks5h"


class DataSourceProxyError(OSError):
    """A safe proxy configuration, handshake, or connection failure."""


@dataclass(frozen=True)
class Socks5hProxy:
    host: str
    port: int


def normalize_data_source_proxy_url(value: Any) -> str:
    """Validate one credential-free SOCKS5H URL and return its canonical form."""

    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() != SUPPORTED_PROXY_SCHEME or not parsed.hostname:
        raise ValueError(f"{PROXY_ENV_NAME} 必须是有效的 socks5h://host:port 地址")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError(f"{PROXY_ENV_NAME} 不允许包含凭据或路径")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{PROXY_ENV_NAME} 不允许包含查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{PROXY_ENV_NAME} 端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"{PROXY_ENV_NAME} 必须包含 1 到 65535 的端口")
    host = parsed.hostname
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{SUPPORTED_PROXY_SCHEME}://{rendered_host}:{port}"


def _configured_proxy(env: Mapping[str, str] | None = None) -> Socks5hProxy | None:
    values = os.environ if env is None else env
    normalized = normalize_data_source_proxy_url(values.get(PROXY_ENV_NAME, ""))
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    host = str(parsed.hostname or "")
    # A Compose container cannot reach the host through its own loopback.
    if values.get("NIUONE_CONTAINER_DATA_DIR") and host in {"127.0.0.1", "localhost", "::1"}:
        host = _container_default_gateway() or "host.docker.internal"
    return Socks5hProxy(host=host, port=int(parsed.port or 0))


def _container_default_gateway(route_path: str = "/proc/net/route") -> str:
    """Return the current Linux container gateway without assuming a Docker subnet."""

    try:
        with open(route_path, encoding="ascii") as route_file:
            rows = route_file.read().splitlines()[1:]
    except OSError:
        return ""
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            packed = struct.pack("<L", int(fields[2], 16))
            gateway = socket.inet_ntoa(packed)
        except (OSError, ValueError, struct.error):
            continue
        if flags & 0x2 and gateway != "0.0.0.0":
            return gateway
    return ""


def resolve_data_source_proxy_url(env: Mapping[str, str] | None = None) -> str:
    """Return the effective proxy URL, including the Compose host translation."""

    proxy = _configured_proxy(env)
    if proxy is None:
        return ""
    rendered_host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
    return f"{SUPPORTED_PROXY_SCHEME}://{rendered_host}:{proxy.port}"


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DataSourceProxyError("SOCKS5H 代理在握手完成前关闭连接")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _connect_socks5h(
    proxy: Socks5hProxy,
    target_host: str,
    target_port: int,
    timeout: float | None,
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _read_exact(sock, 2) != b"\x05\x00":
            raise DataSourceProxyError("SOCKS5H 代理不接受无认证连接")
        encoded_host = target_host.encode("idna")
        if not encoded_host or len(encoded_host) > 255:
            raise DataSourceProxyError("目标数据源域名无效")
        request = (
            b"\x05\x01\x00\x03"
            + bytes((len(encoded_host),))
            + encoded_host
            + struct.pack("!H", target_port)
        )
        sock.sendall(request)
        header = _read_exact(sock, 4)
        if header[:3] != b"\x05\x00\x00":
            reply = header[1] if len(header) > 1 else -1
            raise DataSourceProxyError(f"SOCKS5H 代理连接目标失败（reply={reply}）")
        address_type = header[3]
        if address_type == 1:
            _read_exact(sock, 4)
        elif address_type == 3:
            _read_exact(sock, _read_exact(sock, 1)[0])
        elif address_type == 4:
            _read_exact(sock, 16)
        else:
            raise DataSourceProxyError("SOCKS5H 代理返回未知地址类型")
        _read_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _request_parts(request: str | urllib.request.Request) -> tuple[urllib.request.Request, SplitResult]:
    resolved = request if isinstance(request, urllib.request.Request) else urllib.request.Request(str(request))
    parsed = urlsplit(resolved.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("数据源 URL 必须是有效的 http(s) 地址")
    return resolved, parsed


def _open_via_proxy(
    request: str | urllib.request.Request,
    *,
    proxy: Socks5hProxy,
    timeout: float | None,
    context: ssl.SSLContext | None,
) -> http.client.HTTPResponse:
    resolved, parsed = _request_parts(request)
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = _connect_socks5h(proxy, str(parsed.hostname), target_port, timeout)
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        ssl_context = context or ssl.create_default_context()
        try:
            sock = ssl_context.wrap_socket(sock, server_hostname=str(parsed.hostname))
        except Exception:
            sock.close()
            raise
        connection = http.client.HTTPSConnection(str(parsed.hostname), target_port, timeout=timeout)
    else:
        connection = http.client.HTTPConnection(str(parsed.hostname), target_port, timeout=timeout)
    connection.sock = sock
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = dict(resolved.header_items())
    connection.request(
        resolved.get_method(),
        path,
        body=resolved.data,
        headers=headers,
    )
    response = connection.getresponse()
    if response.status >= 400:
        raise urllib.error.HTTPError(
            resolved.full_url,
            response.status,
            response.reason,
            response.headers,
            response,
        )
    return response


def data_source_urlopen(
    request: str | urllib.request.Request,
    data: bytes | None = None,
    timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT,
    *,
    context: ssl.SSLContext | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Open a data-source request directly or through the configured proxy."""

    resolved = request
    if data is not None:
        if isinstance(request, urllib.request.Request):
            resolved = urllib.request.Request(
                request.full_url,
                data=data,
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
        else:
            resolved = urllib.request.Request(str(request), data=data)
    proxy = _configured_proxy(env)
    if proxy is None:
        if context is None:
            return urllib.request.urlopen(resolved, timeout=timeout)
        return urllib.request.urlopen(resolved, timeout=timeout, context=context)
    normalized_timeout = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else float(timeout)
    return _open_via_proxy(
        resolved,
        proxy=proxy,
        timeout=normalized_timeout,
        context=context,
    )
