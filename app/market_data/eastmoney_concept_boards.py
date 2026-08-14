"""Bounded Eastmoney concept-board ranking used as an intraday cross-check."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from app.market_data.data_source_proxy import data_source_urlopen
except ImportError:  # pragma: no cover - legacy top-level import path
    from market_data.data_source_proxy import data_source_urlopen


EASTMONEY_CONCEPT_BOARD_URLS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
EASTMONEY_CONCEPT_BOARD_FILTER = "m:90+t:3"
EASTMONEY_CONCEPT_BOARD_FIELDS = (
    "f12,f14,f2,f3,f62,f104,f105,f106,f128,f140,f141,f136,f124"
)
EASTMONEY_CONCEPT_BOARD_SOURCE = "eastmoney_concept_board_rank"
EASTMONEY_CONCEPT_BOARD_SOURCE_URL = (
    "https://quote.eastmoney.com/center/boardlist.html"
)
EASTMONEY_CONCEPT_BOARD_SCHEMA_VERSION = 1
EASTMONEY_CONCEPT_BOARD_LIMIT = 100
EASTMONEY_CONCEPT_BOARD_TIMEOUT_SECONDS = 6.0
EASTMONEY_CONCEPT_BOARD_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EASTMONEY_CONCEPT_BOARD_CACHE_TTL_SECONDS = 60.0
EASTMONEY_CONCEPT_BOARD_STALE_TTL_SECONDS = 10 * 60.0
EASTMONEY_CONCEPT_BOARD_FAILURE_TTL_SECONDS = 30.0
_CN_TIMEZONE = timezone(timedelta(hours=8))


class EastmoneyConceptBoardError(RuntimeError):
    """Raised when the bounded concept-board snapshot is unavailable or malformed."""


def _text(value: Any, limit: int = 80) -> str:
    result = re.sub(r"\s+", "", str(value or "")).strip()
    if result.lower() in {"", "-", "--", "nan", "none", "null"}:
        return ""
    return result[:limit]


def _code(value: Any) -> str:
    matched = re.search(r"\d{1,6}", str(value or ""))
    return matched.group(0).zfill(6) if matched else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int:
    number = _number(value)
    return max(0, int(number or 0))


def normalize_eastmoney_concept_name(value: Any) -> str:
    """Normalize exact board labels without attempting ambiguous fuzzy matching."""

    label = re.sub(r"[\s·•_\-—（）()]", "", _text(value)).casefold()
    for suffix in ("概念板块", "概念"):
        if label.endswith(suffix) and len(label) > len(suffix):
            return label[: -len(suffix)]
    return label


@dataclass(frozen=True)
class EastmoneyConceptBoard:
    code: str
    name: str
    rank: int
    change_pct: float | None
    main_net_yi: float | None
    up_count: int
    down_count: int
    flat_count: int
    leader_code: str
    leader_name: str
    leader_market: int | None
    leader_change_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "normalized_name": normalize_eastmoney_concept_name(self.name),
            "rank": self.rank,
            "change_pct": self.change_pct,
            "main_net_yi": self.main_net_yi,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "flat_count": self.flat_count,
            "leader_code": self.leader_code,
            "leader_name": self.leader_name,
            "leader_market": self.leader_market,
            "leader_change_pct": self.leader_change_pct,
        }


@dataclass(frozen=True)
class EastmoneyConceptBoardSignal:
    captured_at: str
    quote_generated_at: str
    total_count: int
    boards: tuple[EastmoneyConceptBoard, ...]
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EASTMONEY_CONCEPT_BOARD_SCHEMA_VERSION,
            "source": EASTMONEY_CONCEPT_BOARD_SOURCE,
            "source_url": EASTMONEY_CONCEPT_BOARD_SOURCE_URL,
            "captured_at": self.captured_at,
            "quote_generated_at": self.quote_generated_at,
            "sort": "change_pct_desc",
            "total_count": self.total_count,
            "covered_count": len(self.boards),
            "stale": self.stale,
            "boards": [board.to_dict() for board in self.boards],
        }


def parse_eastmoney_concept_board_payload(
    payload: Mapping[str, Any],
    *,
    captured_at: str,
) -> EastmoneyConceptBoardSignal:
    """Parse the bounded top-ranked concept rows returned by Eastmoney."""

    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("diff") if isinstance(data, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise EastmoneyConceptBoardError("Eastmoney returned no concept boards")
    try:
        total_count = max(len(rows), int(data.get("total") or 0))
    except (TypeError, ValueError, OverflowError):
        total_count = len(rows)

    boards: list[EastmoneyConceptBoard] = []
    seen_codes: set[str] = set()
    quote_timestamps: list[int] = []
    for row in rows[:EASTMONEY_CONCEPT_BOARD_LIMIT]:
        if not isinstance(row, Mapping):
            continue
        board_code = _text(row.get("f12"), 16)
        name = _text(row.get("f14"))
        if not board_code or not name or board_code in seen_codes:
            continue
        seen_codes.add(board_code)
        main_net = _number(row.get("f62"))
        leader_market_number = _number(row.get("f141"))
        timestamp = _integer(row.get("f124"))
        if timestamp > 0:
            quote_timestamps.append(timestamp)
        boards.append(EastmoneyConceptBoard(
            code=board_code,
            name=name,
            rank=len(boards) + 1,
            change_pct=_number(row.get("f3")),
            main_net_yi=(round(main_net / 100_000_000, 4) if main_net is not None else None),
            up_count=_integer(row.get("f104")),
            down_count=_integer(row.get("f105")),
            flat_count=_integer(row.get("f106")),
            leader_code=_code(row.get("f140")),
            leader_name=_text(row.get("f128"), 40),
            leader_market=(
                int(leader_market_number)
                if leader_market_number is not None
                else None
            ),
            leader_change_pct=_number(row.get("f136")),
        ))
    if not boards:
        raise EastmoneyConceptBoardError(
            "Eastmoney concept-board snapshot contained no valid rows"
        )

    captured = str(captured_at or "")[:19]
    quote_generated_at = captured
    if quote_timestamps:
        try:
            quote_generated_at = datetime.fromtimestamp(
                max(quote_timestamps), tz=_CN_TIMEZONE
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            pass
    return EastmoneyConceptBoardSignal(
        captured_at=captured,
        quote_generated_at=quote_generated_at,
        total_count=total_count,
        boards=tuple(boards),
    )


def _download_payload(
    url: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> Mapping[str, Any]:
    params = {
        "pn": "1",
        "pz": str(EASTMONEY_CONCEPT_BOARD_LIMIT),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": EASTMONEY_CONCEPT_BOARD_FILTER,
        "fields": EASTMONEY_CONCEPT_BOARD_FIELDS,
    }
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0 NiuOne/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": EASTMONEY_CONCEPT_BOARD_SOURCE_URL,
            "Connection": "close",
        },
    )
    with opener(request, timeout=max(1.0, float(timeout_seconds))) as response:
        body = response.read(EASTMONEY_CONCEPT_BOARD_MAX_RESPONSE_BYTES + 1)
    if len(body) > EASTMONEY_CONCEPT_BOARD_MAX_RESPONSE_BYTES:
        raise EastmoneyConceptBoardError(
            "Eastmoney concept-board response exceeded 2 MiB"
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EastmoneyConceptBoardError(
            "Eastmoney returned invalid concept-board JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise EastmoneyConceptBoardError(
            "Eastmoney returned an invalid concept-board payload"
        )
    return parsed


def fetch_eastmoney_concept_board_signal(
    *,
    timeout_seconds: float = EASTMONEY_CONCEPT_BOARD_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = data_source_urlopen,
    now: datetime | None = None,
) -> EastmoneyConceptBoardSignal:
    """Fetch the first 100 concepts ranked by current change percentage."""

    last_error: Exception | None = None
    for url in EASTMONEY_CONCEPT_BOARD_URLS:
        try:
            payload = _download_payload(
                url,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
            captured_at = (now or datetime.now(_CN_TIMEZONE)).astimezone(
                _CN_TIMEZONE
            ).strftime("%Y-%m-%d %H:%M:%S")
            return parse_eastmoney_concept_board_payload(
                payload,
                captured_at=captured_at,
            )
        except Exception as exc:
            last_error = exc
    raise EastmoneyConceptBoardError(
        f"Eastmoney concept-board ranking is unavailable ({type(last_error).__name__})"
    ) from last_error


class EastmoneyConceptBoardSignalCache:
    """Small in-process cache that bounds refreshes and permits brief stale fallback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signal: EastmoneyConceptBoardSignal | None = None
        self._loaded_monotonic = 0.0
        self._next_retry_monotonic = 0.0

    def load(
        self,
        *,
        ttl_seconds: float = EASTMONEY_CONCEPT_BOARD_CACHE_TTL_SECONDS,
        stale_ttl_seconds: float = EASTMONEY_CONCEPT_BOARD_STALE_TTL_SECONDS,
        failure_ttl_seconds: float = EASTMONEY_CONCEPT_BOARD_FAILURE_TTL_SECONDS,
        fetcher: Callable[[], EastmoneyConceptBoardSignal] = fetch_eastmoney_concept_board_signal,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> EastmoneyConceptBoardSignal:
        with self._lock:
            current = float(monotonic())
            age = current - self._loaded_monotonic
            if self._signal is not None and age <= max(0.0, float(ttl_seconds)):
                return copy.deepcopy(self._signal)
            if current < self._next_retry_monotonic:
                if self._signal is not None and age <= max(0.0, float(stale_ttl_seconds)):
                    return self._stale_copy(self._signal)
                raise EastmoneyConceptBoardError(
                    "Eastmoney concept-board refresh is in failure backoff"
                )
            try:
                signal = fetcher()
            except Exception:
                self._next_retry_monotonic = current + max(
                    0.0, float(failure_ttl_seconds)
                )
                if self._signal is not None and age <= max(0.0, float(stale_ttl_seconds)):
                    return self._stale_copy(self._signal)
                raise
            self._signal = signal
            self._loaded_monotonic = current
            self._next_retry_monotonic = 0.0
            return copy.deepcopy(signal)

    @staticmethod
    def _stale_copy(
        signal: EastmoneyConceptBoardSignal,
    ) -> EastmoneyConceptBoardSignal:
        return EastmoneyConceptBoardSignal(
            captured_at=signal.captured_at,
            quote_generated_at=signal.quote_generated_at,
            total_count=signal.total_count,
            boards=signal.boards,
            stale=True,
        )


_SIGNAL_CACHE = EastmoneyConceptBoardSignalCache()


def load_eastmoney_concept_board_signal() -> EastmoneyConceptBoardSignal:
    """Load the current signal through the process-wide bounded cache."""

    return _SIGNAL_CACHE.load()


__all__ = [
    "EASTMONEY_CONCEPT_BOARD_CACHE_TTL_SECONDS",
    "EASTMONEY_CONCEPT_BOARD_SOURCE",
    "EastmoneyConceptBoard",
    "EastmoneyConceptBoardError",
    "EastmoneyConceptBoardSignal",
    "EastmoneyConceptBoardSignalCache",
    "fetch_eastmoney_concept_board_signal",
    "load_eastmoney_concept_board_signal",
    "normalize_eastmoney_concept_name",
    "parse_eastmoney_concept_board_payload",
]
