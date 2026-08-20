#!/usr/bin/env python3
import concurrent.futures
import json
import os
import sys
import threading
import time
import tempfile
import types
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app"
COMPAT = SRC / "compat"
ENTRYPOINTS = SRC / "entrypoints"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(COMPAT))

import multi_strategy_screen as screen  # noqa: E402


class MultiStrategyRuleTests(unittest.TestCase):
    def setUp(self):
        self._saved_strategy_source = os.environ.get(screen.STRATEGY_SOURCE_ENV)
        self._saved_active_strategy = os.environ.pop(screen.ACTIVE_STRATEGY_ENV, None)
        os.environ[screen.STRATEGY_SOURCE_ENV] = "builtin"

    def tearDown(self):
        if self._saved_strategy_source is None:
            os.environ.pop(screen.STRATEGY_SOURCE_ENV, None)
        else:
            os.environ[screen.STRATEGY_SOURCE_ENV] = self._saved_strategy_source
        if self._saved_active_strategy is None:
            os.environ.pop(screen.ACTIVE_STRATEGY_ENV, None)
        else:
            os.environ[screen.ACTIVE_STRATEGY_ENV] = self._saved_active_strategy

    def test_build_market_snapshot_reuses_full_quote_batch(self):
        snapshot = screen.build_market_snapshot({
            "sh600001": {"price": 11.0, "prev_close": 10.0, "change_pct": 10.0, "amount": 2e8, "quote_time": "20260710100001"},
            "sh600002": {"price": 9.0, "prev_close": 10.0, "change_pct": -10.0, "amount": 1e8, "quote_time": "20260710100002"},
            "sz000001": {"price": 10.1, "prev_close": 10.0, "change_pct": 1.0, "amount": 3e8, "quote_time": "20260710100003"},
            "sz000002": {"price": 10.0, "prev_close": 10.0, "change_pct": 0.0, "amount": 4e8, "quote_time": "20260710100004"},
        }, captured_at="2026-07-10 10:00:05", pool_count=5)

        self.assertEqual(snapshot["universe"], "mainboard_non_st")
        self.assertEqual(snapshot["sample_count"], 4)
        self.assertEqual(snapshot["pool_count"], 5)
        self.assertEqual(snapshot["coverage"], 0.8)
        self.assertEqual((snapshot["up"], snapshot["down"], snapshot["flat"]), (2, 1, 1))
        self.assertEqual((snapshot["limit_up"], snapshot["limit_down"]), (1, 1))
        self.assertEqual(snapshot["quote_time"], "2026-07-10 10:00:04")
        self.assertEqual(snapshot["total_amount"], 1e9)

    def test_market_snapshot_uses_board_specific_limit_thresholds(self):
        snapshot = screen.build_market_snapshot({
            "sh600001": {"name": "主板", "price": 11.0, "prev_close": 10.0, "change_pct": 10.0},
            "sz300001": {"name": "创业十点", "price": 11.0, "prev_close": 10.0, "change_pct": 10.0},
            "sz300002": {"name": "创业涨停", "price": 12.0, "prev_close": 10.0, "change_pct": 20.0},
            "sh688001": {"name": "科创跌停", "price": 8.0, "prev_close": 10.0, "change_pct": -20.0},
            "sh600002": {"name": "*ST测试", "price": 10.5, "prev_close": 10.0, "change_pct": 5.0},
        }, stock_universe="st,chi_next,star_market,main_board")

        self.assertEqual(snapshot["limit_up"], 3)
        self.assertEqual(snapshot["limit_down"], 1)

    def test_niuone_uses_full_reference_but_configured_trade_universe(self):
        trade, reference = screen.scan_stock_universes(
            {"niu_leader": object()},
            "main_board",
        )

        self.assertEqual(trade, ("main_board",))
        self.assertEqual(reference, ("chi_next", "star_market", "main_board"))

        trade, reference = screen.scan_stock_universes(
            {"shaofu_b1": object()},
            "main_board,st",
        )
        self.assertEqual(reference, trade)

    def test_niuone_candidate_projection_preserves_five_stage_contract(self):
        projected = screen.niuone_lifecycle_candidate_metadata({
            "niuone_lifecycle_stage": "divergence",
            "niuone_lifecycle_label": "主线分歧",
            "niuone_lifecycle_order": 40,
            "niuone_lifecycle_entry_policy": (
                "selective_repair_reclaim_or_reduce"
            ),
            "ignored": "value",
        })

        self.assertEqual(projected, {
            "niuone_lifecycle_stage": "divergence",
            "niuone_lifecycle_label": "主线分歧",
            "niuone_lifecycle_order": 40,
            "niuone_lifecycle_entry_policy": (
                "selective_repair_reclaim_or_reduce"
            ),
        })

    def test_independent_mainline_mode_ignores_active_trading_strategy(self):
        os.environ[screen.ACTIVE_STRATEGY_ENV] = "zettaranc"

        scorers = screen.strategy_scorers_for_run(niuone_mainline_only=True)

        self.assertTrue(screen.niuone_mainline_only_mode(["--json", "--niuone-mainline-only"]))
        self.assertEqual(set(scorers), set(screen.NIUONE_STRATEGY_IDS))
        self.assertFalse(set(scorers).intersection(screen.ZETTARANC_STRATEGY_IDS))

        source = (SRC / "screening" / "multi_strategy.py").read_text(
            encoding="utf-8"
        )
        mainline_only_branch = source.split(
            "    if niuone_mainline_only:\n"
            "        if niuone_context is None:",
            1,
        )[1].split("    def analyze_candidate", 1)[0]
        self.assertNotIn(
            "fetch_sector_tide_news_precheck",
            mainline_only_branch,
        )

    def test_niuone_scan_never_enters_news_or_model_precheck(self):
        source = (SRC / "screening" / "multi_strategy.py").read_text(
            encoding="utf-8"
        )
        post_scoring = source.split(
            "    # Sort: best_score desc, above_bbi bonus, closer to BBI better",
            1,
        )[1].split(
            "    display_candidates = select_display_candidates(results)",
            1,
        )[0]

        self.assertEqual(
            post_scoring.count("fetch_sector_tide_news_precheck(news_shortlist)"),
            1,
        )
        self.assertNotIn("niuone_news_shortlist", post_scoring)
        self.assertNotIn("elif niuone_enabled", post_scoring)

    def test_kline_prewarm_mode_is_independent_cli_task(self):
        self.assertTrue(screen.kline_prewarm_only_mode(["--json", "--prewarm-kline-cache"]))
        self.assertFalse(screen.kline_prewarm_only_mode(["--json", "--niuone-mainline-only"]))

    def test_prepare_strategy_rows_prefers_cache_and_merges_live_quote(self):
        historical = [
            {
                "date": f"2026-{5 + index // 28:02d}-{index % 28 + 1:02d}",
                "open": 10 + index / 100,
                "close": 10 + index / 100,
                "high": 10.1 + index / 100,
                "low": 9.9 + index / 100,
                "volume": 1000 + index,
            }
            for index in range(60)
        ]
        historical[-1]["date"] = "2026-07-28"
        network_calls = []
        fetched = []

        rows = screen.prepare_strategy_rows(
            "600001",
            "sh600001",
            quote={
                "quote_time": "20260729100501",
                "open": 11.0,
                "price": 11.5,
                "high": 11.8,
                "low": 10.9,
                "volume": 8888,
            },
            historical_rows=historical,
            kline_loader=lambda *_args: network_calls.append(True) or [],
            fetched_callback=lambda symbol, values: fetched.append((symbol, values)),
        )

        self.assertEqual(network_calls, [])
        self.assertEqual(fetched, [])
        self.assertEqual(rows[-1]["date"], "2026-07-29")
        self.assertEqual(rows[-1]["close"], 11.5)
        self.assertIn("ema20", rows[-1])

    def test_prompt_only_preparation_skips_legacy_indicator_enrichment(self):
        historical = [
            {
                "date": f"2026-07-{index + 1:02d}",
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0,
                "volume": 1000,
            }
            for index in range(30)
        ]
        original = screen.enrich_rows
        calls = []
        screen.enrich_rows = lambda rows: calls.append(len(rows))
        try:
            rows = screen.prepare_strategy_rows(
                "600001",
                "sh600001",
                historical_rows=historical,
                enrich_legacy_indicators=False,
            )
        finally:
            screen.enrich_rows = original

        self.assertIsNotNone(rows)
        self.assertEqual(calls, [])
        self.assertEqual(rows[-1]["symbol_code"], "600001")

    def test_prepare_strategy_rows_fills_cache_only_after_network_fallback(self):
        historical = [
            {
                "date": f"2026-{5 + index // 28:02d}-{index % 28 + 1:02d}",
                "open": 10.0,
                "close": 10.0,
                "high": 10.1,
                "low": 9.9,
                "volume": 1000,
            }
            for index in range(60)
        ]
        historical[-1]["date"] = "2026-07-28"
        fetched = []

        rows = screen.prepare_strategy_rows(
            "600001",
            "sh600001",
            quote={"quote_time": "20260729100501", "price": 10.2},
            kline_loader=lambda *_args: historical,
            fetched_callback=lambda symbol, values: fetched.append((symbol, len(values))),
        )

        self.assertIsNotNone(rows)
        self.assertEqual(fetched, [("sh600001", 60)])

    def test_independent_mainline_news_shortlist_uses_theme_context(self):
        shortlist = screen.niuone_news_shortlist({
            "themes": {
                "银行": {
                    "industry": "银行",
                    "strong_stocks": [
                        {"code": "600036", "name": "招商银行", "strong_score": 88},
                        {"code": "601398", "name": "工商银行", "strong_score": 72},
                    ],
                },
                "电力": {
                    "industry": "电力",
                    "strong_stocks": [
                        {"code": "600036", "name": "重复股票", "strong_score": 99},
                        {"code": "600011", "name": "华能国际", "strong_score": 81},
                    ],
                },
            },
        }, limit=2)

        self.assertEqual(
            [(item["code"], item["industry"]) for item in shortlist],
            [("600036", "银行"), ("600011", "电力")],
        )

    def test_niuone_trade_pool_filter_ignores_turnover_and_daily_move(self):
        candidates = [
            ("600001", "零成交额"),
            ("300001", "下跌股票"),
            ("688001", "缺少涨幅"),
            ("600002", "无效价格"),
            ("300002", "缺失行情"),
        ]
        keys = {code: ("sh" if code.startswith(("6", "9")) else "sz") + code for code, _ in candidates}
        quotes = {
            "sh600001": {"price": 10.0, "amount": 0.0, "change_pct": 0.0},
            "sz300001": {"price": 10.0, "amount": 1.0, "change_pct": -9.0},
            "sh688001": {"price": 10.0, "amount": 0.0},
            "sh600002": {"price": 0.0, "amount": 9e8, "change_pct": 10.0},
        }

        selected = screen.filter_niuone_reference_candidates(candidates, keys, quotes)

        self.assertEqual([item[0] for item in selected], ["600001", "300001", "688001"])

    def test_niuone_dates_follow_quote_date_and_exact_previous_trading_day(self):
        status_calls = []

        def status_loader(value, *, allow_refresh=True):
            status_calls.append((value, allow_refresh))
            return {"previous_trading_day": "2026-07-24"}

        current, previous = screen.resolve_niuone_trading_dates(
            [
                {
                    "quote": {"quote_time": "20260727103000"},
                    "rows": [{"date": "2026-07-24"}],
                },
                {
                    "quote": {"quote_time": "20260727103100"},
                    "rows": [{"date": "2026-07-24"}],
                },
            ],
            status_loader=status_loader,
        )

        self.assertEqual((current, previous), ("2026-07-27", "2026-07-24"))
        self.assertEqual(status_calls, [("2026-07-27", False)])

    def test_niuone_dates_parse_iso_quote_times_and_recover_previous_weekday(self):
        current, previous = screen.resolve_niuone_trading_dates(
            [
                {"quote": {"quote_time": "2026-08-19 10:05:01"}},
                {"quote": {"quote_time": "2026/08/19 10:05:02"}},
            ],
            status_loader=lambda *_args, **_kwargs: {"previous_trading_day": ""},
        )

        self.assertEqual((current, previous), ("2026-08-19", "2026-08-18"))

    def test_scan_accepted_kline_dates_keep_previous_close_when_quotes_are_live(self):
        accepted = screen.scan_accepted_kline_dates(
            "2026-08-19",
            "",
            now=datetime(2026, 8, 19, 10, 5, 0),
            status_loader=lambda *_args, **_kwargs: {
                "date": "2026-08-19",
                "is_trading_day": True,
                "previous_trading_day": "",
            },
        )

        self.assertEqual(accepted, {"2026-08-18", "2026-08-19"})

    def test_ready_cache_coverage_keeps_previous_close_when_live_bars_are_partial(self):
        from app.market_data import tencent_kline_cache as cache

        def rows_ending(last_day: str) -> list[dict]:
            series = [
                {
                    "date": f"2026-{5 + index // 28:02d}-{index % 28 + 1:02d}",
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "volume": 1000,
                }
                for index in range(40)
            ]
            series[-1]["date"] = last_day
            return series

        with tempfile.TemporaryDirectory(prefix="niuone-kline-coverage-") as directory:
            path = Path(directory) / "daily.sqlite3"
            today_symbols = [f"sh600{index:03d}" for index in range(47)]
            previous_symbols = [f"sz000{index:03d}" for index in range(53)]
            cache.store_kline_series(
                {
                    **{symbol: rows_ending("2026-08-19") for symbol in today_symbols},
                    **{symbol: rows_ending("2026-08-18") for symbol in previous_symbols},
                },
                path=path,
            )
            quotes = {
                symbol: {"quote_time": "2026-08-19 10:05:01"}
                for symbol in [*today_symbols, *previous_symbols]
            }
            as_of, previous = screen.resolve_quote_trading_dates(
                quotes,
                status_loader=lambda *_args, **_kwargs: {"previous_trading_day": ""},
            )
            accepted = screen.scan_accepted_kline_dates(
                as_of,
                previous,
                now=datetime(2026, 8, 19, 10, 5, 0),
                status_loader=lambda *_args, **_kwargs: {
                    "date": "2026-08-19",
                    "is_trading_day": True,
                    "previous_trading_day": "",
                },
            )
            loaded = cache.load_kline_series_map(
                [*today_symbols, *previous_symbols],
                path=path,
                accepted_last_dates=accepted,
                min_rows=30,
            )
            coverage = len(loaded) / 100

        self.assertEqual((as_of, previous), ("2026-08-19", "2026-08-18"))
        self.assertEqual(accepted, {"2026-08-18", "2026-08-19"})
        self.assertGreaterEqual(coverage, 0.9)
        self.assertEqual(len(loaded), 100)

    def test_niuone_previous_context_uses_newest_persisted_sample(self):
        with tempfile.TemporaryDirectory(prefix="niuone-context-") as directory:
            root = Path(directory)
            minute = root / "niuone_mainline_minute_latest.json"
            dedicated = root / "niuone_mainline_latest.json"
            shared = root / "multi_strategy_latest.json"
            minute.write_text(
                '{"generated_at":"2026-07-29 10:25:00","niuone_context":{"as_of_date":"2026-07-29","sample_at":"2026-07-29 10:25:00","mainline":{"primary":"半导体"}}}',
                encoding="utf-8",
            )
            dedicated.write_text(
                '{"generated_at":"2026-07-29 10:00:00","niuone_context":{"as_of_date":"2026-07-29","sample_at":"2026-07-29 10:00:00","mainline":{"primary":"银行"}}}',
                encoding="utf-8",
            )
            shared.write_text(
                '{"generated_at":"2026-07-29 10:10:00","niuone_context":{"as_of_date":"2026-07-29","sample_at":"2026-07-29 10:10:00","mainline":{"primary":"证券"}}}',
                encoding="utf-8",
            )
            original_minute = screen.NIUONE_MAINLINE_MINUTE_CACHE
            original_dedicated = screen.NIUONE_MAINLINE_CACHE
            original_shared = screen.MULTI_STRATEGY_CACHE
            try:
                screen.NIUONE_MAINLINE_MINUTE_CACHE = minute
                screen.NIUONE_MAINLINE_CACHE = dedicated
                screen.MULTI_STRATEGY_CACHE = shared
                context = screen.load_previous_niuone_context()
            finally:
                screen.NIUONE_MAINLINE_MINUTE_CACHE = original_minute
                screen.NIUONE_MAINLINE_CACHE = original_dedicated
                screen.MULTI_STRATEGY_CACHE = original_shared

        self.assertEqual(context["as_of_date"], "2026-07-29")
        self.assertEqual(context["mainline"]["primary"], "半导体")

    def test_scan_outputs_include_bounded_candidate_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="niuone-scan-output-") as directory:
            root = Path(directory)
            originals = {
                "B1_OUTPUT_DIR": screen.B1_OUTPUT_DIR,
                "B1_CACHE_FILE": screen.B1_CACHE_FILE,
                "MULTI_STRATEGY_CACHE": screen.MULTI_STRATEGY_CACHE,
                "PRACTICE_CANDIDATES_CACHE": screen.PRACTICE_CANDIDATES_CACHE,
                "B1_HISTORY_DIR": screen.B1_HISTORY_DIR,
                "MULTI_STRATEGY_HISTORY": screen.MULTI_STRATEGY_HISTORY,
            }
            try:
                screen.B1_OUTPUT_DIR = root
                screen.B1_CACHE_FILE = root / "b1_screen_latest.json"
                screen.MULTI_STRATEGY_CACHE = root / "multi_strategy_latest.json"
                screen.PRACTICE_CANDIDATES_CACHE = root / "practice_candidates_latest.json"
                screen.B1_HISTORY_DIR = root / "b1_history"
                screen.MULTI_STRATEGY_HISTORY = root / "multi_strategy_history"
                legacy_archive = (
                    screen.B1_HISTORY_DIR
                    / "2026-08-03"
                    / "2026-08-03_14-50-00.json"
                )
                legacy_archive.parent.mkdir(parents=True)
                legacy_archive.write_text("{}\n", encoding="utf-8")
                screen.write_outputs(
                    {
                        "generated_at": "2026-08-04 10:00:00",
                        "items": [{"code": "600001"}],
                        "trade_items": [],
                        "niuone_context": {
                            "stocks": {"600001": {"private": "large"}}
                        },
                    },
                    "2026-08-04 10:00:00",
                )
                compact = json.loads(
                    screen.PRACTICE_CANDIDATES_CACHE.read_text(encoding="utf-8")
                )
                primary_archive = (
                    screen.MULTI_STRATEGY_HISTORY
                    / "2026-08-04"
                    / "2026-08-04_10-00-00.json"
                )
                self.assertTrue(primary_archive.exists())
                self.assertFalse(legacy_archive.exists())
                self.assertFalse(
                    (
                        screen.B1_HISTORY_DIR
                        / "2026-08-04"
                        / "2026-08-04_10-00-00.json"
                    ).exists()
                )
            finally:
                for name, value in originals.items():
                    setattr(screen, name, value)

        self.assertEqual(compact["items"], [{"code": "600001"}])
        self.assertEqual(compact["trade_items"], [])
        self.assertNotIn("niuone_context", compact)

    def test_scan_history_cleanup_retires_legacy_and_bounds_primary(self):
        with tempfile.TemporaryDirectory(prefix="niuone-scan-history-") as directory:
            root = Path(directory)
            legacy = root / "b1_history"
            primary = root / "multi_strategy_history"

            def archive(base: Path, date: str, time_value: str) -> Path:
                path = base / date / f"{date}_{time_value}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
                return path

            archive(legacy, "2026-08-03", "10-00-00")
            archive(legacy, "2026-08-04", "09-25-00")
            legacy_unknown = legacy / "2026-08-04" / "keep.txt"
            legacy_unknown.write_text("preserve\n", encoding="utf-8")

            archive(primary, "2026-08-03", "10-00-00")
            archive(primary, "2026-08-03", "14-50-00")
            archive(primary, "2026-08-04", "09-25-00")
            archive(primary, "2026-08-04", "10-00-00")
            archive(primary, "2026-08-04", "10-30-00")
            archive(primary, "2026-08-04", "11-00-00")
            primary_unknown = primary / "2026-08-03" / "manual-note.json"
            primary_unknown.write_text("{}\n", encoding="utf-8")

            result = screen.cleanup_scan_history(
                "2026-08-04",
                legacy_history_dir=legacy,
                primary_history_dir=primary,
                retention_dates=1,
                max_files_per_date=2,
            )

            self.assertEqual(result["legacy_removed"], 2)
            self.assertEqual(result["primary_removed"], 4)
            self.assertTrue(legacy_unknown.exists())
            self.assertTrue(primary_unknown.exists())
            self.assertEqual(
                sorted(path.name for path in (primary / "2026-08-04").glob("*.json")),
                [
                    "2026-08-04_10-30-00.json",
                    "2026-08-04_11-00-00.json",
                ],
            )

    @staticmethod
    def _tencent_quote_response():
        parts = [""] * 39
        parts[1] = "测试股票"
        parts[3] = "10.50"
        parts[4] = "10.00"
        parts[6] = "1000"
        parts[30] = "20260717100000"
        parts[33] = "10.60"
        parts[34] = "9.90"
        parts[37] = "10000"
        parts[38] = "2.5"
        payload = f'v_sh600000="{"~".join(parts)}";'.encode("gbk")

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return payload

        return Response()

    def test_tencent_batch_quote_retries_timeout_then_succeeds(self):
        calls = []
        delays = []
        original_urlopen = screen.urllib.request.urlopen

        def fake_urlopen(_request, timeout=0):
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return self._tencent_quote_response()

        try:
            screen.urllib.request.urlopen = fake_urlopen
            result = screen.tencent_batch_quote(
                ["sh600000"],
                timeout_seconds=2,
                max_attempts=3,
                backoff_seconds=0.25,
                batch_label="2/21",
                sleep_fn=delays.append,
            )
        finally:
            screen.urllib.request.urlopen = original_urlopen

        self.assertEqual(calls, [2, 2])
        self.assertEqual(delays, [0.25])
        self.assertEqual(result["sh600000"]["price"], 10.5)

    def test_tencent_batch_quote_reports_batch_after_retry_budget_exhausted(self):
        calls = []
        delays = []
        original_urlopen = screen.urllib.request.urlopen

        def fake_urlopen(_request, timeout=0):
            calls.append(timeout)
            raise urllib.error.URLError(TimeoutError("timed out"))

        try:
            screen.urllib.request.urlopen = fake_urlopen
            with self.assertRaisesRegex(
                screen.TencentQuoteBatchError,
                r"batch=7/21 failed after 3/3 attempts: timeout",
            ):
                screen.tencent_batch_quote(
                    ["sh600000"],
                    timeout_seconds=2,
                    max_attempts=3,
                    backoff_seconds=0.25,
                    batch_label="7/21",
                    sleep_fn=delays.append,
                )
        finally:
            screen.urllib.request.urlopen = original_urlopen

        self.assertEqual(calls, [2, 2, 2])
        self.assertEqual(delays, [0.25, 0.5])

    def test_tencent_batch_quote_does_not_retry_nonretryable_http_error(self):
        calls = []
        original_urlopen = screen.urllib.request.urlopen

        def fake_urlopen(request, timeout=0):
            calls.append(timeout)
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        try:
            screen.urllib.request.urlopen = fake_urlopen
            with self.assertRaisesRegex(screen.TencentQuoteBatchError, r"1/3 attempts: HTTP 403"):
                screen.tencent_batch_quote(
                    ["sh600000"],
                    timeout_seconds=2,
                    max_attempts=3,
                    sleep_fn=lambda _delay: self.fail("HTTP 403 must not be retried"),
                )
        finally:
            screen.urllib.request.urlopen = original_urlopen

        self.assertEqual(calls, [2])

    def test_quote_request_timeout_is_bounded_by_remaining_stage_budget(self):
        self.assertEqual(
            screen.bounded_quote_request_timeout(
                90,
                max_attempts=3,
                backoff_seconds=0.5,
            ),
            10,
        )
        self.assertAlmostEqual(
            screen.bounded_quote_request_timeout(
                6,
                max_attempts=3,
                backoff_seconds=0.5,
            ),
            1.5,
        )

    def test_sector_tide_loads_only_exact_previous_trading_day_snapshot(self):
        calls = {}

        def status_loader(value, *, allow_refresh=True):
            calls["status_value"] = value
            calls["allow_refresh"] = allow_refresh
            return {
                "previous_trading_day": "2026-07-16",
                "source": "test_calendar",
            }

        def snapshot_reader(path, *, trade_date):
            calls["snapshot_path"] = path
            calls["trade_date"] = trade_date
            return {
                "available": True,
                "snapshot": True,
                "source": "同花顺问财",
                "date": trade_date,
                "items": [{"code": "600000.SH"}],
            }

        snapshot_path = Path("/tmp/niuone-sector-tide-dragon-tiger-latest.json")
        payload = screen.load_previous_sector_tide_dragon_tiger(
            datetime(2026, 7, 17, 10, 0, 0),
            snapshot_path=snapshot_path,
            status_loader=status_loader,
            snapshot_reader=snapshot_reader,
        )

        self.assertFalse(calls["allow_refresh"])
        self.assertEqual(calls["trade_date"], "2026-07-16")
        self.assertEqual(calls["snapshot_path"], snapshot_path)
        self.assertEqual(payload["date"], "2026-07-16")
        self.assertEqual(payload["requested_date"], "2026-07-16")
        self.assertEqual(payload["calendar_source"], "test_calendar")

    def test_sector_tide_missing_previous_snapshot_degrades_to_neutral(self):
        requested = []

        payload = screen.load_previous_sector_tide_dragon_tiger(
            datetime(2026, 7, 17, 10, 0, 0),
            snapshot_path=Path("/tmp/niuone-sector-tide-dragon-tiger-latest.json"),
            status_loader=lambda _value, **_kwargs: {
                "previous_trading_day": "2026-07-16",
                "source": "test_calendar",
            },
            snapshot_reader=lambda _path, *, trade_date: requested.append(trade_date),
        )

        self.assertEqual(requested, ["2026-07-16"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["error"], "snapshot_missing")
        self.assertEqual(payload["items"], [])

    def test_sector_tide_loads_validated_overnight_us_cache(self):
        calls = []
        current = datetime(2026, 7, 17, 9, 30, 0)
        payload = screen.load_sector_tide_overnight_us(
            current,
            summary_loader=lambda now: calls.append(now) or {
                "available": True,
                "source": "overnight_us_market_summary",
                "target_cn_date": "2026-07-17",
                "target_us_date": "2026-07-16",
                "tone": "offensive",
                "sector_mappings": [],
            },
        )

        self.assertEqual(calls, [current])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["target_us_date"], "2026-07-16")
        self.assertEqual(payload["tone"], "offensive")

    def test_sector_tide_missing_overnight_us_cache_degrades_to_neutral(self):
        payload = screen.load_sector_tide_overnight_us(
            datetime(2026, 7, 17, 9, 30, 0),
            summary_loader=lambda _now: None,
        )

        self.assertFalse(payload["available"])
        self.assertEqual(payload["error"], "cache_missing_or_stale")

    def test_sector_tide_fetches_structured_news_for_at_most_five_candidates(self):
        candidates = [
            {"code": f"00000{index}", "name": f"测试{index}"}
            for index in range(1, 7)
        ]
        config = screen.NewsPrecheckConfig(
            base_url="https://news.example/v1",
            api_key="secret",
            model="search-model",
        )
        captured = {}

        def fetcher(selected, active_config, **kwargs):
            captured["selected"] = selected
            captured["config"] = active_config
            captured["kwargs"] = kwargs
            return [
                {
                    "code": item["code"],
                    "name": item["name"],
                    "checked": True,
                    "available": True,
                    "tone": "neutral",
                    "tone_label": "中性",
                    "summary": "最近3天无明确重大消息（中性）",
                    "fetched_at": "2026-07-17T09:30:00+08:00",
                }
                for item in selected
            ]

        payload = screen.fetch_sector_tide_news_precheck(
            candidates,
            datetime(2026, 7, 17, 9, 30, 0),
            config=config,
            fetcher=fetcher,
        )

        self.assertEqual(len(captured["selected"]), 5)
        self.assertIs(captured["config"], config)
        self.assertEqual(captured["kwargs"]["max_candidates"], 5)
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["records"]), 5)

    def test_sector_tide_news_failure_degrades_without_blocking_scan(self):
        config = screen.NewsPrecheckConfig(
            base_url="https://news.example/v1",
            api_key="secret",
            model="search-model",
        )
        payload = screen.fetch_sector_tide_news_precheck(
            [{"code": "000001", "name": "平安银行"}],
            config=config,
            fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        )

        self.assertTrue(payload["configured"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["error"], "fetch_TimeoutError")

    def test_stock_universe_classifies_boards_and_st_as_additive_scopes(self):
        self.assertEqual(
            screen.normalize_stock_universe("main_board,ST,chi_next"),
            "st,chi_next,main_board",
        )
        self.assertTrue(screen.stock_in_universe("600000", "浦发银行", "main_board"))
        self.assertFalse(screen.stock_in_universe("300001", "特锐德", "main_board"))
        self.assertTrue(screen.stock_in_universe("300001", "特锐德", "chi_next"))
        self.assertTrue(screen.stock_in_universe("688001", "华兴源创", "star_market"))
        self.assertFalse(screen.stock_in_universe("600001", "ST测试", "main_board"))
        self.assertTrue(screen.stock_in_universe("600001", "ST测试", "st"))
        self.assertTrue(screen.stock_in_universe("300002", "*ST测试", "st"))
        with self.assertRaises(ValueError):
            screen.normalize_stock_universe("")
        with self.assertRaises(ValueError):
            screen.normalize_stock_universe("beijing")

    def test_market_snapshot_records_non_default_stock_universe(self):
        snapshot = screen.build_market_snapshot(
            {},
            stock_universe="st,chi_next,star_market,main_board",
        )

        self.assertEqual(snapshot["universe"], "configured_a_share")
        self.assertEqual(snapshot["stock_universe"], ["st", "chi_next", "star_market", "main_board"])
        self.assertEqual(snapshot["stock_universe_label"], "ST、创业板、科创板、主板")

    def test_code_pool_applies_configured_boards_and_st_scope(self):
        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return enumerate(self.rows)

        sh_calls = []
        fake_akshare = types.SimpleNamespace(
            stock_info_sh_name_code=lambda symbol: (
                sh_calls.append(symbol)
                or FakeFrame(
                    [{"证券代码": "600001", "证券简称": "主板测试"}]
                    if symbol == "主板A股"
                    else [{"证券代码": "688001", "证券简称": "科创测试"}]
                )
            ),
            stock_info_sz_name_code=lambda symbol: FakeFrame([
                {"A股代码": "000001", "A股简称": "深主板"},
                {"A股代码": "300001", "A股简称": "创业测试"},
                {"A股代码": "300002", "A股简称": "*ST创业"},
                {"A股代码": "920001", "A股简称": "北交测试"},
            ]),
            stock_info_a_code_name=lambda: FakeFrame([]),
        )
        original = sys.modules.get("akshare")
        sys.modules["akshare"] = fake_akshare
        try:
            pool = screen.load_a_share_code_pool("st,star_market,main_board")
        finally:
            if original is None:
                sys.modules.pop("akshare", None)
            else:
                sys.modules["akshare"] = original

        self.assertEqual(sh_calls, ["主板A股", "科创板"])
        self.assertEqual(pool, [
            ("000001", "深主板"),
            ("300002", "*ST创业"),
            ("600001", "主板测试"),
            ("688001", "科创测试"),
        ])

    def test_build_index_risk_snapshot_counts_core_indices_below_ma20(self):
        quotes = {
            "sh000001": {"price": 9.8, "change_pct": -1.2},
            "sz399001": {"price": 9.7, "change_pct": -1.5},
            "sz399006": {"price": 10.2, "change_pct": -0.2},
        }
        rows = [{"close": 10.0} for _ in range(21)]

        snapshot = screen.build_index_risk_snapshot(quotes, kline_loader=lambda symbol, count: rows)

        self.assertEqual(snapshot["core_index_count"], 3)
        self.assertEqual(snapshot["index_below_ma20_count"], 2)
        self.assertAlmostEqual(snapshot["index_average_change_pct"], -0.967, places=3)

    def test_recent_b1_indices_require_core_negative_j(self):
        rows = [{"j": None, "open": 10.0, "close": 10.0} for _ in range(10)]
        rows[4]["j"] = -9.5
        rows[6]["j"] = -10.5

        self.assertEqual(screen.recent_b1_indices(rows, lookback=9, end_offset=1), [6])

    def test_b2_confirmation_rejects_b1_older_than_three_days(self):
        rows = [
            {"open": 10.0, "close": 10.0, "high": 10.1, "low": 9.9, "volume": 100, "j": 20.0, "bbi": 10.0, "change_pct": 0.0}
            for _ in range(40)
        ]
        rows[35]["j"] = -12.0
        rows[-1].update({"open": 10.0, "close": 10.5, "high": 10.6, "low": 9.95, "volume": 150, "j": 40.0, "bbi": 10.0, "change_pct": 5.0})

        self.assertIsNone(screen.score_b2_confirm(rows))

        rows[35]["j"] = 20.0
        rows[36]["j"] = -12.0
        result = screen.score_b2_confirm(rows)
        self.assertIsNotNone(result)
        self.assertEqual(result["days_from_b1"], 3)

    def test_zettaranc_prefers_higher_industry_main_flow_rank(self):
        rows = [
            {
                "open": 10.0,
                "close": 10.0,
                "high": 10.1,
                "low": 9.9,
                "volume": 100,
                "j": 20.0,
                "bbi": 10.0,
                "change_pct": 0.0,
            }
            for _ in range(40)
        ]
        rows[36]["j"] = -12.0
        rows[-1].update({
            "open": 10.0,
            "close": 10.5,
            "high": 11.2,
            "low": 9.95,
            "volume": 110,
            "j": 50.0,
            "bbi": 10.0,
            "change_pct": 5.0,
            "industry": "半导体行业",
        })
        inflow = [
            {"name": name, "net_flow_yi": 100 - index * 5}
            for index, name in enumerate([
                "半导体", "通信设备", "银行", "证券", "软件开发",
                "汽车零部件", "电池", "消费电子", "光伏设备", "家电",
            ])
        ]
        context = {
            "industry_money_flow": {
                "metric": "industry_main_net_flow",
                "source": "东方财富行业板块主力净额",
                "generated_at": "2026-07-22 10:00:00",
                "inflow": inflow,
            },
        }

        high_rank = screen.analyze_enriched_rows(
            rows,
            {"b2_confirm": screen.score_b2_confirm},
            context,
        )["strategies"]["b2_confirm"]
        low_rank_rows = [dict(row) for row in rows]
        low_rank_rows[-1]["industry"] = "家电"
        low_rank = screen.analyze_enriched_rows(
            low_rank_rows,
            {"b2_confirm": screen.score_b2_confirm},
            context,
        )["strategies"]["b2_confirm"]

        self.assertEqual(high_rank["score_before_industry_flow"], 9.0)
        self.assertEqual(high_rank["industry_flow_rank"], 1)
        self.assertEqual(high_rank["industry_flow_adjustment"], 1.5)
        self.assertEqual(high_rank["score"], 10.0)
        self.assertEqual(low_rank["industry_flow_rank"], 10)
        self.assertEqual(low_rank["industry_flow_adjustment"], 0.15)
        self.assertEqual(low_rank["score"], 9.2)
        self.assertGreater(high_rank["decision_score"], low_rank["decision_score"])

    def test_zettaranc_ignores_stale_industry_flow_fallback(self):
        rows = [{"industry": "半导体"}]
        stale = screen.zettaranc_industry_flow_signal(rows, {
            "industry_money_flow": {
                "metric": "industry_main_net_flow",
                "stale_cache": True,
                "error": "request timeout",
                "inflow": [{"name": "半导体", "net_flow_yi": 100}],
            },
        })

        self.assertFalse(stale["industry_flow_available"])
        self.assertFalse(stale["industry_flow_matched"])
        self.assertEqual(stale["industry_flow_adjustment"], 0.0)

    def test_zettaranc_exposes_matching_industry_outflow_without_score_penalty(self):
        rows = [{"industry": "半导体行业"}]
        signal = screen.zettaranc_industry_flow_signal(rows, {
            "industry_money_flow": {
                "metric": "industry_main_net_flow",
                "source": "东方财富行业板块主力净额",
                "generated_at": "2026-07-22 10:00:00",
                "inflow": [{"name": "银行", "net_flow_yi": 10.0}],
                "outflow": [
                    {"name": "软件开发", "net_flow_yi": -30.0},
                    {"name": "半导体", "net_flow_yi": -20.0},
                ],
            },
        })

        self.assertTrue(signal["industry_flow_available"])
        self.assertFalse(signal["industry_flow_matched"])
        self.assertTrue(signal["industry_outflow_matched"])
        self.assertEqual(signal["industry_flow_direction"], "outflow")
        self.assertEqual(signal["industry_outflow_rank"], 2)
        self.assertEqual(signal["industry_outflow_net_yi"], -20.0)
        self.assertEqual(signal["industry_flow_adjustment"], 0.0)

    def test_n_structure_filter_uses_local_swing_lows(self):
        rising = [{"low": low} for low in [10.4, 10.0, 9.5, 9.8, 10.5, 10.2, 10.0, 10.3, 10.8]]
        falling = [{"low": low} for low in [10.4, 10.0, 9.5, 9.8, 10.5, 9.4, 9.2, 9.5, 10.0]]

        self.assertTrue(screen.n_structure_ok(rising, lookback=20))
        self.assertFalse(screen.n_structure_ok(falling, lookback=20))

    def test_shaofu_b1_above_core_j_is_watch_only(self):
        payload = screen.with_strategy_profile("shaofu_b1", {
            "score": 9.0,
            "distance_pct": 1.0,
            "current_j": -5.0,
            "vol_shrink": True,
            "pullback_shrink": True,
            "n_structure": True,
            "bull_rope": True,
            "stop_space_pct": 4.0,
            "pressure_space_pct": 8.0,
            "risk_flags": [],
        })

        self.assertFalse(payload["actionable"])
        self.assertIn("B1核心J未≤-10", payload["hard_blockers"])

    def test_select_trade_candidates_excludes_hard_blocked_items(self):
        good = {
            "code": "600001",
            "best_score": 9.0,
            "entry_threshold": 8.0,
            "distance_pct": 1.0,
            "actionable": True,
            "hard_blockers": [],
        }
        blocked = {
            "code": "600002",
            "best_score": 9.5,
            "entry_threshold": 8.0,
            "distance_pct": 1.0,
            "actionable": False,
            "hard_blockers": ["B1核心J未≤-10"],
        }

        self.assertEqual(screen.select_trade_candidates([blocked, good]), [good])

    def test_candidate_lists_sort_by_displayed_score_descending(self):
        candidates = [
            {
                "code": "600001",
                "best_score": 6.6,
                "best_decision_score": 9.8,
                "entry_threshold": 6.0,
                "distance_pct": 1.0,
                "actionable": True,
                "hard_blockers": [],
            },
            {
                "code": "600002",
                "best_score": 8.3,
                "best_decision_score": 8.5,
                "entry_threshold": 6.0,
                "distance_pct": 1.0,
                "actionable": True,
                "hard_blockers": [],
            },
            {
                "code": "600003",
                "best_score": 7.4,
                "best_decision_score": 9.0,
                "entry_threshold": 6.0,
                "distance_pct": 1.0,
                "actionable": True,
                "hard_blockers": [],
            },
        ]

        expected = ["600002", "600003", "600001"]
        self.assertEqual(
            [item["code"] for item in screen.select_display_candidates(candidates)],
            expected,
        )
        self.assertEqual(
            [item["code"] for item in screen.select_trade_candidates(candidates)],
            expected,
        )

    def test_candidate_counts_follow_runtime_settings(self):
        candidates = [
            {
                "code": f"600{i:03d}",
                "best_score": 10.0 - i / 100,
                "entry_threshold": 8.0,
                "distance_pct": 1.0,
                "actionable": True,
                "hard_blockers": [],
                "best_strategy": "shaofu_b1",
            }
            for i in range(20)
        ]
        display_name = "DASHBOARD_DISPLAY_CANDIDATE_LIMIT"
        trade_name = "DASHBOARD_TRADE_CANDIDATE_LIMIT"
        saved_display = os.environ.get(display_name)
        saved_trade = os.environ.get(trade_name)
        try:
            os.environ[display_name] = "12"
            os.environ[trade_name] = "5"
            self.assertEqual(len(screen.select_display_candidates(candidates)), 12)
            self.assertEqual(len(screen.select_trade_candidates(candidates)), 5)
            self.assertEqual(len(screen.select_display_candidates(candidates, limit=7)), 7)
            self.assertEqual(len(screen.select_trade_candidates(candidates, limit=3)), 3)
        finally:
            if saved_display is None:
                os.environ.pop(display_name, None)
            else:
                os.environ[display_name] = saved_display
            if saved_trade is None:
                os.environ.pop(trade_name, None)
            else:
                os.environ[trade_name] = saved_trade

    def test_market_enrichment_reuses_market_wide_downloads_across_workers(self):
        import pandas as pd

        margin_calls = []
        block_calls = []
        margin_frame = pd.DataFrame([
            {
                '标的证券代码': '600001',
                '融资买入额': 4_000_000,
                '融资偿还额': 1_000_000,
                '融资余额': 100_000_000,
            },
            {
                '标的证券代码': '600002',
                '融资买入额': 2_000_000,
                '融资偿还额': 1_000_000,
                '融资余额': 100_000_000,
            },
        ])
        block_frame = pd.DataFrame([
            {'证券代码': '600001', '成交额': 1_000_000, '折溢率': 2.5},
            {'证券代码': '600002', '成交额': 2_000_000, '折溢率': -1.0},
        ])

        def load_margin(date):
            time.sleep(0.03)
            margin_calls.append(date)
            return margin_frame

        def load_block(**kwargs):
            time.sleep(0.03)
            block_calls.append(kwargs)
            return block_frame

        fake_akshare = types.SimpleNamespace(
            stock_margin_detail_sse=load_margin,
            stock_margin_detail_szse=lambda date: margin_calls.append(date) or margin_frame,
            stock_dzjy_mrmx=load_block,
        )
        original_akshare = sys.modules.get('akshare')
        screen._MARGIN_DETAIL_CACHE.clear()
        screen._BLOCK_TRADE_CACHE.clear()
        sys.modules['akshare'] = fake_akshare
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                margin_results = list(pool.map(screen.get_margin_signal, ['600001', '600002']))
                block_results = list(
                    pool.map(screen.get_block_trade_signal, ['600001', '600002'])
                )
        finally:
            screen._MARGIN_DETAIL_CACHE.clear()
            screen._BLOCK_TRADE_CACHE.clear()
            if original_akshare is None:
                sys.modules.pop('akshare', None)
            else:
                sys.modules['akshare'] = original_akshare

        self.assertTrue(all(result is not None for result in margin_results))
        self.assertTrue(all(result is not None for result in block_results))
        self.assertEqual(len(margin_calls), 1)
        self.assertEqual(len(block_calls), 1)

    def test_eastmoney_board_annotation_adds_industry_and_concepts(self):
        candidates = [{
            "code": "000977",
            "name": "浪潮信息",
            "best_strategy": "niu_leader",
            "signal_theme": "存储芯片",
        }]
        original_loader = screen.load_bulk_stock_board_map
        original_save = screen.save_stock_industry_cache
        try:
            screen.load_bulk_stock_board_map = lambda _codes: {
                "000977": screen.EastmoneyStockBoard(
                    code="000977",
                    industry="计算机设备",
                    concepts=("存储芯片", "先进封装概念"),
                )
            }
            screen.save_stock_industry_cache = lambda _cache: None
            screen.annotate_candidate_industries(candidates)
        finally:
            screen.load_bulk_stock_board_map = original_loader
            screen.save_stock_industry_cache = original_save

        self.assertEqual(candidates[0]["industry"], "计算机设备")
        self.assertEqual(candidates[0]["sector"], "计算机设备")
        self.assertEqual(candidates[0]["themes"], ["存储芯片", "先进封装"])
        self.assertEqual(candidates[0]["signal_theme"], "存储芯片")

    def test_annotate_candidate_industries_adds_sector_alias_once(self):
        display = [{"code": "600001", "name": "测试A"}]
        trade = [{"code": "600001", "name": "测试A"}]
        calls: list[str] = []

        def fake_lookup(code: str) -> str:
            calls.append(code)
            return "银行板块"

        screen.annotate_candidate_industries(display, trade, lookup=fake_lookup)

        self.assertEqual(calls, ["600001"])
        self.assertEqual(display[0]["industry"], "银行")
        self.assertEqual(display[0]["sector"], "银行")
        self.assertEqual(trade[0]["industry"], "银行")
        self.assertEqual(trade[0]["sector"], "银行")

    def test_bulk_industry_lookup_prevents_unbounded_per_stock_fallbacks(self):
        candidates = [
            {"code": "600001", "name": "测试A"},
            {"code": "600002", "name": "测试B"},
            {"code": "600003", "name": "测试C"},
        ]
        original_cache = screen._STOCK_INDUSTRY_MEMORY_CACHE
        original_lookup = screen.lookup_stock_industry
        original_save = screen.save_stock_industry_cache
        fallback_calls: list[str] = []
        screen._STOCK_INDUSTRY_MEMORY_CACHE = {"600001": "旧行业"}

        def fake_bulk(codes: set[str]):
            self.assertEqual(codes, {"600001", "600002", "600003"})
            return {"600001": "银行板块", "600002": "半导体行业"}

        try:
            screen.lookup_stock_industry = lambda code: fallback_calls.append(code) or "其他"
            screen.save_stock_industry_cache = lambda _cache: None
            screen.annotate_candidate_industries(
                candidates,
                bulk_lookup=fake_bulk,
                max_fallback_lookups=0,
                max_workers=4,
            )
        finally:
            screen._STOCK_INDUSTRY_MEMORY_CACHE = original_cache
            screen.lookup_stock_industry = original_lookup
            screen.save_stock_industry_cache = original_save

        self.assertEqual(candidates[0]["industry"], "银行")
        self.assertEqual(candidates[1]["industry"], "半导体")
        self.assertNotIn("industry", candidates[2])
        self.assertEqual(fallback_calls, [])

    def test_eastmoney_board_annotation_uses_one_batch(self):
        candidates = [
            {"code": "600001", "name": "测试A"},
            {"code": "600002", "name": "测试B"},
        ]
        calls: list[set[str]] = []
        original_loader = screen.load_bulk_stock_board_map
        original_save = screen.save_stock_industry_cache

        try:
            def fake_loader(codes):
                calls.append(set(codes))
                return {
                    code: screen.EastmoneyStockBoard(
                        code=code,
                        industry="银行",
                        concepts=("中特估",),
                    )
                    for code in codes
                }
            screen.load_bulk_stock_board_map = fake_loader
            screen.save_stock_industry_cache = lambda _cache: None
            screen.annotate_candidate_industries(candidates, max_workers=2)
        finally:
            screen.load_bulk_stock_board_map = original_loader
            screen.save_stock_industry_cache = original_save

        self.assertEqual(calls, [{"600001", "600002"}])
        self.assertTrue(all(candidate["industry"] == "银行" for candidate in candidates))
        self.assertTrue(all(candidate["themes"] == ["中特估"] for candidate in candidates))

    def test_eastmoney_board_failure_is_fail_closed(self):
        candidates = [
            {"code": "600001", "name": "测试A"},
            {"code": "600002", "name": "测试B"},
        ]
        original_loader = screen.load_bulk_stock_board_map
        try:
            screen.load_bulk_stock_board_map = lambda _codes: (_ for _ in ()).throw(
                RuntimeError("eastmoney unavailable")
            )
            with self.assertRaisesRegex(RuntimeError, "eastmoney unavailable"):
                screen.annotate_candidate_industries(candidates, max_workers=2)
        finally:
            screen.load_bulk_stock_board_map = original_loader

    def test_persona_strategies_are_registered(self):
        old = os.environ.get(screen.PERSONA_STRATEGY_ENV)
        try:
            os.environ.pop(screen.PERSONA_STRATEGY_ENV, None)
            self.assertIn("li_daxiao_bottom", screen.STRATEGY_META)
            self.assertNotIn("buffett_value", screen.STRATEGY_META)
            self.assertIn("li_daxiao_bottom", screen.STRATEGY_SCORERS)
            self.assertNotIn("buffett_value", screen.STRATEGY_SCORERS)
            self.assertEqual(screen.STRATEGY_META["shaofu_b1"]["family"], "persona")
            self.assertEqual(screen.STRATEGY_META["breakout"]["family"], "local")
            self.assertEqual(screen.enabled_persona_strategy_ids(), {"niuone"})
        finally:
            if old is None:
                os.environ.pop(screen.PERSONA_STRATEGY_ENV, None)
            else:
                os.environ[screen.PERSONA_STRATEGY_ENV] = old

    def test_active_strategy_scorers_follow_suite_setting(self):
        old = os.environ.get(screen.ACTIVE_STRATEGY_ENV)
        try:
            os.environ[screen.ACTIVE_STRATEGY_ENV] = "base"
            active = screen.active_strategy_scorers()
            self.assertNotIn("buffett_value", active)
            self.assertNotIn("li_daxiao_bottom", active)
            self.assertNotIn("shaofu_b1", active)
            self.assertIn("trend_pullback", active)
            self.assertIn("breakout", active)

            os.environ[screen.ACTIVE_STRATEGY_ENV] = "zettaranc"
            active = screen.active_strategy_scorers()
            self.assertNotIn("buffett_value", active)
            self.assertNotIn("li_daxiao_bottom", active)
            self.assertNotIn("trend_pullback", active)
            self.assertNotIn("breakout", active)
            self.assertIn("shaofu_b1", active)
            self.assertIn("b3_accelerate", active)

            os.environ[screen.ACTIVE_STRATEGY_ENV] = "li_daxiao_bottom"
            active = screen.active_strategy_scorers()
            self.assertIn("li_daxiao_bottom", active)
            self.assertNotIn("shaofu_b1", active)
            self.assertNotIn("b3_accelerate", active)
            self.assertNotIn("trend_pullback", active)
            self.assertNotIn("breakout", active)

            os.environ[screen.ACTIVE_STRATEGY_ENV] = "base"
            active = screen.active_strategy_scorers()
            self.assertIn("trend_pullback", active)
            self.assertIn("breakout", active)
            self.assertNotIn("li_daxiao_bottom", active)
            self.assertNotIn("shaofu_b1", active)

        finally:
            if old is None:
                os.environ.pop(screen.ACTIVE_STRATEGY_ENV, None)
            else:
                os.environ[screen.ACTIVE_STRATEGY_ENV] = old

    def test_preset_text_suite_uses_only_independent_prompt_scorer(self):
        old = os.environ.get(screen.ACTIVE_STRATEGY_ENV)
        try:
            os.environ[screen.ACTIVE_STRATEGY_ENV] = "preset_text"
            active = screen.active_strategy_scorers()

            self.assertNotIn("li_daxiao_bottom", active)
            self.assertNotIn("shaofu_b1", active)
            self.assertNotIn("trend_pullback", active)
            self.assertNotIn("breakout", active)
            self.assertEqual(set(active), {"preset_text"})
        finally:
            if old is None:
                os.environ.pop(screen.ACTIVE_STRATEGY_ENV, None)
            else:
                os.environ[screen.ACTIVE_STRATEGY_ENV] = old

    def test_preset_text_scorer_exposes_neutral_facts_without_base_entry_gates(self):
        rows = []
        for index in range(40):
            close = 10.0 + index * 0.05
            rows.append({
                "date": f"2026-06-{index + 1:02d}",
                "open": close - 0.02,
                "high": close + 0.12,
                "low": close - 0.10,
                "close": close,
                "volume": 1000 + index * 20,
            })
        screen.enrich_rows(rows)
        rows[-1].update({
            "quote_amount": 1_500_000_000,
            "quote_turnover": 3.2,
            "quote_change_pct": 1.1,
        })

        scored = screen.STRATEGY_SCORERS["preset_text"](rows)

        self.assertEqual(scored["strategy_id"], "preset_text")
        self.assertTrue(scored["actionable"])
        self.assertEqual(scored["hard_blockers"], [])
        self.assertEqual(scored["entry_threshold"], 0.0)
        self.assertIsNone(scored["distance_pct"])
        self.assertIsNotNone(scored["return_20d_pct"])
        self.assertIsNotNone(scored["distance_ema20_pct"])
        self.assertIsNotNone(scored["volume_ratio_5d"])
        selected = screen.select_trade_candidates([
            {
                "code": "600000",
                "name": "测试股",
                "best_strategy": "preset_text",
                "best_score": scored["score"],
                **scored,
            }
        ], limit=1)
        self.assertEqual([item["code"] for item in selected], ["600000"])

    def test_active_strategy_suites_are_isolated(self):
        old = os.environ.get(screen.ACTIVE_STRATEGY_ENV)
        try:
            expected = {
                "base": {"breakout", "trend_pullback"},
                "zettaranc": {"b3_accelerate", "b2_confirm", "shaofu_b1", "super_b1"},
                "li_daxiao_bottom": {"li_daxiao_bottom"},
                "niuone": {"niu_emerging", "niu_leader", "niu_pullback", "niu_reversal_probe"},
                "preset_text": {"preset_text"},
            }
            for suite, scorer_ids in expected.items():
                os.environ[screen.ACTIVE_STRATEGY_ENV] = suite
                self.assertEqual(set(screen.active_strategy_scorers()), scorer_ids)
        finally:
            if old is None:
                os.environ.pop(screen.ACTIVE_STRATEGY_ENV, None)
            else:
                os.environ[screen.ACTIVE_STRATEGY_ENV] = old

    def test_li_daxiao_profile_applies_hard_blockers(self):
        payload = screen.with_strategy_profile("li_daxiao_bottom", {
            "score": 9.0,
            "distance_pct": 1.0,
            "bottom_zone": False,
            "stabilizing": False,
            "bluechip_liquidity_proxy": True,
            "value_anchor_proxy": True,
            "anti_black_five_proxy": True,
            "not_fresh_listing_proxy": True,
            "no_chase_zone": True,
            "speculation_heat": False,
            "breakdown_risk": False,
            "volatility_20d_pct": 2.0,
            "risk_flags": [],
        })

        self.assertFalse(payload["actionable"])
        self.assertIn("未处低位区", payload["hard_blockers"])
        self.assertIn("底部未企稳", payload["hard_blockers"])

    def test_li_daxiao_profile_blocks_speculative_chasing(self):
        payload = screen.with_strategy_profile("li_daxiao_bottom", {
            "score": 9.0,
            "distance_pct": 3.2,
            "bottom_zone": True,
            "stabilizing": True,
            "bluechip_liquidity_proxy": True,
            "value_anchor_proxy": False,
            "anti_black_five_proxy": False,
            "not_fresh_listing_proxy": False,
            "no_chase_zone": False,
            "speculation_heat": True,
            "breakdown_risk": False,
            "volatility_20d_pct": 2.0,
            "risk_flags": [],
        })

        self.assertFalse(payload["actionable"])
        self.assertIn("低估蓝筹代理不足", payload["hard_blockers"])
        self.assertIn("黑五类/题材热度代理偏高", payload["hard_blockers"])
        self.assertIn("次新代理风险", payload["hard_blockers"])
        self.assertIn("李大霄不追高", payload["hard_blockers"])
        self.assertIn("换手/涨幅过热", payload["hard_blockers"])


if __name__ == "__main__":
    unittest.main()
