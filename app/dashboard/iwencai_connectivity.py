"""Read-only connectivity checks for the configured iWencai gateway."""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping

from market_data.iwencai_client import (
    IwencaiClient,
    IwencaiConfig,
    IwencaiConfigurationError,
    IwencaiError,
    IwencaiRequestError,
    IwencaiResponseError,
)


IWENCAI_TEST_QUERY = "上证指数最新价"
IWENCAI_TEST_FIELD_NAMES = (
    "IWENCAI_NEWS_PRECHECK_ENABLED",
    "IWENCAI_BASE_URL",
    "IWENCAI_API_KEY",
    "IWENCAI_TIMEOUT_SECONDS",
)
IWENCAI_MESSAGE_TEST_STOCK = {"code": "600519", "name": "贵州茅台"}


def iwencai_test_metadata() -> dict[str, Any]:
    return {
        "id": "iwencai",
        "group_slug": "iwencai",
        "label": "问财接口",
        "description": "验证问财行情接口；开启消息面预检时，同时验证公告、新闻和事件技能。",
        "field_names": list(IWENCAI_TEST_FIELD_NAMES),
    }


def _error_message(exc: IwencaiError) -> str:
    status = exc.status_code
    if status == 400:
        return "问财请求格式不受支持（HTTP 400）"
    if status == 401:
        return "问财 API Key 验证失败（HTTP 401）"
    if status == 403:
        return "问财接口拒绝访问，请检查 API Key 权限（HTTP 403）"
    if status == 404:
        return "问财接口地址不存在（HTTP 404）"
    if status == 429:
        return "问财接口触发限流或额度不足（HTTP 429）"
    if status is not None and 500 <= status <= 599:
        return f"问财服务暂时不可用（HTTP {status}）"
    if exc.code == "api_key_missing":
        return "请先配置问财 API Key"
    if exc.code in {"network_error", "concurrency_timeout"}:
        return "无法连接问财服务，请检查地址和网络"
    if isinstance(exc, IwencaiResponseError):
        return "问财接口已响应，但返回格式无法识别"
    return "问财接口测试失败"


def test_iwencai_connection(
    values: Mapping[str, Any],
    *,
    opener=None,
    semaphore: threading.BoundedSemaphore | None = None,
    monotonic=time.monotonic,
) -> dict[str, Any]:
    """Run bounded read-only queries and return only non-sensitive diagnostics."""

    try:
        configured = IwencaiConfig.from_env(values)
    except IwencaiConfigurationError as exc:
        return {
            "ok": False,
            "target": "iwencai",
            "error": str(exc),
            "error_code": exc.code,
        }

    test_config = IwencaiConfig(
        enabled=True,
        base_url=configured.base_url,
        api_key=configured.api_key,
        timeout_seconds=max(2, min(30, configured.timeout_seconds)),
        max_retries=0,
        max_concurrency=1,
    )
    client_kwargs: dict[str, Any] = {"sleep": lambda _seconds: None}
    if opener is not None:
        client_kwargs["opener"] = opener
    if semaphore is not None:
        client_kwargs["semaphore"] = semaphore
    client = IwencaiClient(test_config, **client_kwargs)
    started = monotonic()
    active_skill = "hithink-market-query"
    try:
        payload = client.query(
            IWENCAI_TEST_QUERY,
            page=1,
            limit=1,
            is_cache=True,
            expand_index=False,
        )
        tested_skills = ["hithink-market-query"]
        news_precheck_enabled = str(
            values.get("IWENCAI_NEWS_PRECHECK_ENABLED") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if news_precheck_enabled:
            label = f"{IWENCAI_MESSAGE_TEST_STOCK['code']} {IWENCAI_MESSAGE_TEST_STOCK['name']}"
            active_skill = "announcement-search"
            client.comprehensive_search(
                f"{label} 最近3日公司公告",
                channel="announcement",
                size=1,
            )
            active_skill = "news-search"
            client.comprehensive_search(
                f"{label} 最近3日公司新闻和重大事项",
                channel="news",
                size=1,
            )
            active_skill = "hithink-event-query"
            client.query(
                f"{label} 最近3日业绩预告或增发或质押或解禁或机构调研或监管函",
                page=1,
                limit=1,
                is_cache=False,
                expand_index=True,
                skill_id="hithink-event-query",
            )
            tested_skills.extend((
                "announcement-search",
                "news-search",
                "hithink-event-query",
            ))
    except (IwencaiConfigurationError, IwencaiRequestError, IwencaiResponseError) as exc:
        return {
            "ok": False,
            "target": "iwencai",
            "error": f"{active_skill}：{_error_message(exc)}",
            "error_code": exc.code,
            "failed_skill": active_skill,
        }
    except Exception:
        return {
            "ok": False,
            "target": "iwencai",
            "error": f"{active_skill}：问财接口测试失败",
            "error_code": "unexpected_error",
            "failed_skill": active_skill,
        }

    elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
    return {
        "ok": True,
        "target": "iwencai",
        "elapsed_ms": elapsed_ms,
        "returned_count": len(payload.get("datas") or []),
        "tested_skills": tested_skills,
        "message": (
            f"问财行情及 3 个消息面技能均已接通（{elapsed_ms} ms）"
            if news_precheck_enabled
            else f"问财行情接口已接通（{elapsed_ms} ms）"
        ),
    }
