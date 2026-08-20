"""A-share trading calendar with a cached data-source lookup."""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__.startswith("app."):
    from ...core.paths import get_dashboard_home
else:
    from core.paths import get_dashboard_home

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_HOME = get_dashboard_home(PROJECT_ROOT)
CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CALENDAR_CACHE_FILE = Path(
    os.environ.get(
        "A_SHARE_TRADING_CALENDAR_CACHE",
        DASHBOARD_HOME / "cron" / "state" / "a_share_trading_calendar.json",
    )
).expanduser()

_LOCAL_SITE_PACKAGES_READY = False
_CACHE_MEMO: dict[str, Any] | None = None
_REFRESH_ATTEMPTED_YEARS: set[int] = set()


def current_cn_date() -> date:
    return datetime.now(CN_TZ).date()


def normalize_date(value: datetime | date | str | None = None) -> date:
    if value is None:
        return current_cn_date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def date_text(value: datetime | date | str | None = None) -> str:
    return normalize_date(value).strftime("%Y-%m-%d")


def add_local_runtime_site_packages() -> None:
    global _LOCAL_SITE_PACKAGES_READY
    if _LOCAL_SITE_PACKAGES_READY:
        return
    _LOCAL_SITE_PACKAGES_READY = True
    site_root = DASHBOARD_HOME.parent / ".venv" / "lib"
    if not site_root.exists():
        return
    for site_packages in sorted(site_root.glob("python*/site-packages"), reverse=True):
        if site_packages.exists() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
            break


def parse_trade_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def fetch_trade_dates_from_akshare() -> tuple[set[str], str]:
    add_local_runtime_site_packages()
    import akshare as ak  # type: ignore

    df = ak.tool_trade_date_hist_sina()
    dates: set[str] = set()
    if df is None:
        return dates, "akshare.tool_trade_date_hist_sina"
    columns = list(getattr(df, "columns", []))
    date_col = "trade_date" if "trade_date" in columns else (columns[0] if columns else "")
    if hasattr(df, "iterrows") and date_col:
        for _, row in df.iterrows():
            text = parse_trade_date(row.get(date_col))
            if text:
                dates.add(text)
    else:
        for item in df:
            text = parse_trade_date(item)
            if text:
                dates.add(text)
    return dates, "akshare.tool_trade_date_hist_sina"


def load_cached_calendar(cache_file: Path | None = None) -> dict[str, Any]:
    global _CACHE_MEMO
    cache_file = cache_file or CALENDAR_CACHE_FILE
    if cache_file == CALENDAR_CACHE_FILE and _CACHE_MEMO is not None:
        return dict(_CACHE_MEMO)
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    dates = sorted({item for item in (parse_trade_date(value) for value in payload.get("dates", [])) if item})
    loaded = {
        "dates": dates,
        "source": payload.get("source") or "",
        "updated_at": payload.get("updated_at") or "",
    }
    if cache_file == CALENDAR_CACHE_FILE:
        _CACHE_MEMO = loaded
    return dict(loaded)


def save_cached_calendar(dates: set[str], source: str, cache_file: Path | None = None) -> dict[str, Any]:
    global _CACHE_MEMO
    cache_file = cache_file or CALENDAR_CACHE_FILE
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "dates": sorted(dates),
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(cache_file.suffix + ".new")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cache_file)
    if cache_file == CALENDAR_CACHE_FILE:
        _CACHE_MEMO = payload
    return dict(payload)


def refresh_trading_calendar(
    *,
    cache_file: Path | None = None,
    fetcher: Callable[[], tuple[set[str], str]] | None = None,
) -> dict[str, Any]:
    dates, source = (fetcher or fetch_trade_dates_from_akshare)()
    if not dates:
        raise RuntimeError("empty A-share trading calendar")
    return save_cached_calendar(dates, source, cache_file=cache_file)


def cache_has_year(dates: set[str], year: int) -> bool:
    prefix = f"{year:04d}-"
    return any(item.startswith(prefix) for item in dates)


def fallback_previous_weekday(target: date) -> str:
    current = target - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.strftime("%Y-%m-%d")


def _iso_date(value: Any) -> str:
    text = str(value or "")[:10]
    return text if ISO_DATE_RE.fullmatch(text) else ""


def accepted_kline_cache_dates(
    now: datetime | date | str | None = None,
    *,
    extra_dates: Iterable[str] = (),
    status: Mapping[str, Any] | None = None,
    status_loader: Callable[..., Mapping[str, Any]] | None = None,
    cache_file: Path | None = None,
    allow_refresh: bool = False,
) -> set[str]:
    """Return completed-bar dates that remain safe for a live K-line scan."""
    current = normalize_date(now)
    current_text = current.strftime("%Y-%m-%d")
    if status is None:
        try:
            if status_loader is None:
                calendar = trading_day_status(
                    current,
                    cache_file=cache_file,
                    allow_refresh=allow_refresh,
                )
            else:
                calendar = status_loader(current, allow_refresh=allow_refresh)
        except Exception:
            calendar = {
                "date": current_text,
                "is_trading_day": current.weekday() < 5,
                "previous_trading_day": fallback_previous_weekday(current),
            }
    else:
        calendar = status
    accepted = {_iso_date(calendar.get("previous_trading_day"))}
    if not accepted - {""}:
        accepted.add(fallback_previous_weekday(current))
    if calendar.get("is_trading_day"):
        accepted.add(_iso_date(calendar.get("date")) or current_text)
    accepted.update(_iso_date(value) for value in extra_dates)
    return {value for value in accepted if value}


def trading_day_status(
    value: datetime | date | str | None = None,
    *,
    cache_file: Path | None = None,
    allow_refresh: bool = True,
    fetcher: Callable[[], tuple[set[str], str]] | None = None,
) -> dict[str, Any]:
    target = normalize_date(value)
    target_text = target.strftime("%Y-%m-%d")
    cache_file = cache_file or CALENDAR_CACHE_FILE
    payload = load_cached_calendar(cache_file)
    dates = set(payload.get("dates") or [])
    source = payload.get("source") or "cache"

    if allow_refresh and not cache_has_year(dates, target.year) and target.year not in _REFRESH_ATTEMPTED_YEARS:
        _REFRESH_ATTEMPTED_YEARS.add(target.year)
        try:
            payload = refresh_trading_calendar(cache_file=cache_file, fetcher=fetcher)
            dates = set(payload.get("dates") or [])
            source = payload.get("source") or source
        except Exception as exc:
            source = f"weekday_fallback:{type(exc).__name__}"

    if cache_has_year(dates, target.year):
        return {
            "date": target_text,
            "is_trading_day": target_text in dates,
            "previous_trading_day": max((item for item in dates if item < target_text), default=""),
            "next_trading_day": min((item for item in dates if item > target_text), default=""),
            "source": source or "cache",
            "calendar_cached": True,
        }

    return {
        "date": target_text,
        "is_trading_day": target.weekday() < 5,
        "previous_trading_day": fallback_previous_weekday(target),
        "next_trading_day": "",
        "source": source or "weekday_fallback",
        "calendar_cached": False,
    }


def is_a_share_trading_day(value: datetime | date | str | None = None) -> bool:
    return bool(trading_day_status(value).get("is_trading_day"))
