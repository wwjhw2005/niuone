import unittest
import urllib.request
from unittest import mock

from app.market_data.data_source_proxy import (
    PROXY_ENV_NAME,
    data_source_urlopen,
    normalize_data_source_proxy_url,
    resolve_data_source_proxy_url,
)


class _FakeSocket:
    def __init__(self):
        self.received = bytearray(
            b"\x05\x00"  # no-auth greeting accepted
            b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50"  # connect accepted
        )
        self.sent = []
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def sendall(self, value):
        self.sent.append(bytes(value))

    def recv(self, size):
        value = bytes(self.received[:size])
        del self.received[:size]
        return value

    def close(self):
        self.closed = True


class _FakeResponse:
    status = 200
    reason = "OK"
    headers = {}

    def read(self):
        return b'{"ok":true}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeConnection:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.request_args = None
        type(self).instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.request_args = (method, path, body, headers or {})

    def getresponse(self):
        return _FakeResponse()


class DataSourceProxyTests(unittest.TestCase):
    def setUp(self):
        _FakeConnection.instances = []
        self.sock = _FakeSocket()
        self.proxy_env = {PROXY_ENV_NAME: "socks5h://127.0.0.1:10800"}
        self.socket_patch = mock.patch(
            "app.market_data.data_source_proxy.socket.create_connection",
            return_value=self.sock,
        )
        self.connection_patch = mock.patch(
            "app.market_data.data_source_proxy.http.client.HTTPConnection",
            _FakeConnection,
        )
        self.create_connection = self.socket_patch.start()
        self.connection_patch.start()

    def tearDown(self):
        self.connection_patch.stop()
        self.socket_patch.stop()

    def test_socks5h_uses_remote_dns_and_preserves_get_path(self):
        request = urllib.request.Request(
            "http://quote.example.test:8080/path?q=1",
            headers={"User-Agent": "NiuOne-test"},
        )
        with data_source_urlopen(request, timeout=2, env=self.proxy_env) as response:
            self.assertEqual(response.read(), b'{"ok":true}')

        self.create_connection.assert_called_once_with(("127.0.0.1", 10800), timeout=2.0)
        self.assertEqual(self.sock.sent[0], b"\x05\x01\x00")
        self.assertEqual(
            self.sock.sent[1],
            b"\x05\x01\x00\x03\x12quote.example.test\x1f\x90",
        )
        connection = _FakeConnection.instances[0]
        self.assertIs(connection.sock, self.sock)
        method, path, body, headers = connection.request_args
        self.assertEqual((method, path, body), ("GET", "/path?q=1", None))
        self.assertEqual(headers["User-agent"], "NiuOne-test")

    def test_socks5h_preserves_post_body(self):
        request = urllib.request.Request(
            "http://openapi.example.test/v1/query2data",
            data=b'{"query":"test"}',
            headers={"Content-Type": "application/json"},
        )
        with data_source_urlopen(request, timeout=2, env=self.proxy_env) as response:
            self.assertEqual(response.status, 200)
        method, path, body, headers = _FakeConnection.instances[0].request_args
        self.assertEqual((method, path), ("POST", "/v1/query2data"))
        self.assertEqual(body, b'{"query":"test"}')
        self.assertEqual(headers["Content-type"], "application/json")

    def test_empty_proxy_retains_standard_urlopen(self):
        sentinel = object()
        with mock.patch("urllib.request.urlopen", return_value=sentinel) as opener:
            result = data_source_urlopen("https://example.test/", timeout=3, env={})
        self.assertIs(result, sentinel)
        opener.assert_called_once_with("https://example.test/", timeout=3)
        self.create_connection.assert_not_called()

    def test_proxy_url_rejects_credentials_and_non_socks_scheme(self):
        for value in (
            "http://127.0.0.1:10800",
            "socks5h://user:pass@127.0.0.1:10800",
            "socks5h://127.0.0.1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_data_source_proxy_url(value)

    def test_compose_translates_loopback_to_current_network_gateway(self):
        with mock.patch(
            "app.market_data.data_source_proxy._container_default_gateway",
            return_value="172.18.0.1",
        ):
            resolved = resolve_data_source_proxy_url({
                PROXY_ENV_NAME: "socks5h://127.0.0.1:10800",
                "NIUONE_CONTAINER_DATA_DIR": "/data",
            })
        self.assertEqual(resolved, "socks5h://172.18.0.1:10800")

    def test_container_gateway_parses_linux_route_hex(self):
        from app.market_data.data_source_proxy import _container_default_gateway

        route = "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t010012AC\t0003\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=route)):
            self.assertEqual(_container_default_gateway(), "172.18.0.1")


if __name__ == "__main__":
    unittest.main()
