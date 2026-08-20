#!/usr/bin/env python3
"""Compatibility facade for the A-share trading-calendar service."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

if __package__ == "app":
    from .reports.a_share import calendar as _calendar
else:
    from reports.a_share import calendar as _calendar

PROJECT_ROOT = _calendar.PROJECT_ROOT
DASHBOARD_HOME = _calendar.DASHBOARD_HOME
CALENDAR_CACHE_FILE = _calendar.CALENDAR_CACHE_FILE

_normalize_date = _calendar.normalize_date
_date_text = _calendar.date_text
_add_local_runtime_site_packages = _calendar.add_local_runtime_site_packages
_parse_trade_date = _calendar.parse_trade_date
_fetch_trade_dates_from_akshare = _calendar.fetch_trade_dates_from_akshare
_cache_has_year = _calendar.cache_has_year
fallback_previous_weekday = _calendar.fallback_previous_weekday
_fallback_previous_weekday = fallback_previous_weekday
accepted_kline_cache_dates = _calendar.accepted_kline_cache_dates


def load_cached_calendar(cache_file: Path | None = None) -> dict[str, Any]:
    return _calendar.load_cached_calendar(cache_file or CALENDAR_CACHE_FILE)


def save_cached_calendar(dates: set[str], source: str, cache_file: Path | None = None) -> dict[str, Any]:
    return _calendar.save_cached_calendar(dates, source, cache_file or CALENDAR_CACHE_FILE)


def refresh_trading_calendar(
    *,
    cache_file: Path | None = None,
    fetcher: Callable[[], tuple[set[str], str]] | None = None,
) -> dict[str, Any]:
    return _calendar.refresh_trading_calendar(
        cache_file=cache_file or CALENDAR_CACHE_FILE,
        fetcher=fetcher or _fetch_trade_dates_from_akshare,
    )


def trading_day_status(
    value: datetime | date | str | None = None,
    *,
    cache_file: Path | None = None,
    allow_refresh: bool = True,
    fetcher: Callable[[], tuple[set[str], str]] | None = None,
) -> dict[str, Any]:
    return _calendar.trading_day_status(
        value,
        cache_file=cache_file or CALENDAR_CACHE_FILE,
        allow_refresh=allow_refresh,
        fetcher=fetcher or _fetch_trade_dates_from_akshare,
    )


def is_a_share_trading_day(value: datetime | date | str | None = None) -> bool:
    return bool(trading_day_status(value).get("is_trading_day"))
