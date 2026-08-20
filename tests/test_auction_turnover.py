#!/usr/bin/env python3
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.market_data import auction_turnover


def auction_report(amount: str = "100.00亿", samples: int = 5_100) -> str:
    return (
        f"样本 `{samples}` 只 | 高开 `1` · 平开 `2` · 低开 `3`\n"
        f"强高开 `1` · 深低开 `2` | 竞价额 `{amount}` · 竞价量 `1万手`"
    )


def close_report(amount: str = "10000.00亿") -> str:
    return f"涨停 `10` · 跌停 `2` | 成交额 `{amount}`"


class AuctionTurnoverTests(unittest.TestCase):
    def test_extracts_complete_auction_and_close_amounts(self):
        self.assertEqual(
            auction_turnover.extract_auction_turnover_yi(auction_report()),
            100,
        )
        self.assertEqual(
            auction_turnover.extract_close_turnover_yi(close_report("1.20万亿")),
            12_000,
        )

    def test_rejects_post_open_or_incomplete_auction_report(self):
        self.assertIsNone(
            auction_turnover.extract_auction_turnover_yi(
                "今日开盘后补全\n" + auction_report()
            )
        )
        self.assertIsNone(
            auction_turnover.extract_auction_turnover_yi(
                auction_report(samples=3_999)
            )
        )

    def test_builds_shrunken_opening_estimate_from_matched_days(self):
        start = dt.date(2026, 6, 1)
        auction = {}
        close = {}
        for offset in range(11):
            day = (start + dt.timedelta(days=offset)).isoformat()
            auction[day] = 100 + offset
            close[day] = auction[day] * (100 if offset != 5 else 1_000)
        auction["2026-07-01"] = 120

        profile = auction_turnover.build_auction_turnover_profile(
            dt.date(2026, 7, 1),
            auction_by_date=auction,
            close_by_date=close,
        )

        self.assertEqual(profile["profile_days"], 11)
        expected = 10_600 * (120 / 105) ** 0.5
        self.assertEqual(profile["auction_elasticity"], 0.5)
        self.assertEqual(profile["historical_auction_median_yi"], 105)
        self.assertEqual(profile["historical_turnover_median_yi"], 10_600)
        self.assertAlmostEqual(
            profile["opening_estimated_turnover_yi"],
            expected,
            places=2,
        )
        self.assertEqual(profile["daily_profiles"][-1]["turnover_yi"], 11_000)

    def test_shrinkage_dampens_an_out_of_range_low_auction(self):
        start = dt.date(2026, 6, 1)
        auction = {
            (start + dt.timedelta(days=offset)).isoformat(): 100
            for offset in range(10)
        }
        close = {day: 10_000 for day in auction}
        auction["2026-07-01"] = 25

        profile = auction_turnover.build_auction_turnover_profile(
            dt.date(2026, 7, 1),
            auction_by_date=auction,
            close_by_date=close,
        )

        self.assertEqual(profile["opening_estimated_turnover_yi"], 5_000)
        self.assertGreater(profile["opening_estimated_turnover_yi"], 2_500)

    def test_requires_enough_matched_history(self):
        with self.assertRaisesRegex(ValueError, "requires 10"):
            auction_turnover.build_auction_turnover_profile(
                dt.date(2026, 7, 1),
                auction_by_date={"2026-06-30": 100, "2026-07-01": 120},
                close_by_date={"2026-06-30": 10_000},
            )

    def test_loads_structured_current_sample_and_legacy_report_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "auction.json"
            state_path.write_text(json.dumps({
                "samples": [{
                    "date": "2026-07-01",
                    "captured_at": "2026-07-01 09:25:02",
                    "auction_turnover_yi": 120,
                    "quote_count": 5_100,
                }],
            }), encoding="utf-8")
            db_path = root / "reports.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE dashboard_messages "
                "(time_text TEXT, content TEXT, source_id TEXT, timestamp REAL)"
            )
            connection.executemany(
                "INSERT INTO dashboard_messages VALUES (?, ?, ?, ?)",
                [
                    (
                        "2026-06-30 09:25:01",
                        auction_report(),
                        f"cron_output_{auction_turnover.AUCTION_JOB_ID}_legacy",
                        1.0,
                    ),
                    (
                        "2026-06-30 15:10:01",
                        close_report(),
                        f"cron_output_{auction_turnover.CLOSE_JOB_ID}_legacy",
                        2.0,
                    ),
                ],
            )
            connection.commit()
            connection.close()

            auction, close = auction_turnover.load_turnover_report_series(
                db_path=db_path,
                state_path=state_path,
            )

        self.assertEqual(auction["2026-07-01"], 120)
        self.assertEqual(auction["2026-06-30"], 100)
        self.assertEqual(close["2026-06-30"], 10_000)

    def test_loads_structured_close_sample_without_message_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            close_path = root / "close.json"
            auction_turnover.persist_close_turnover_sample(
                generated_at="2026-06-30 15:00:08",
                turnover_yi=12_345.67,
                quote_count=5_200,
                path=close_path,
            )
            auction, close = auction_turnover.load_turnover_report_series(
                db_path=root / "missing.db",
                state_path=root / "missing-auction.json",
                close_state_path=close_path,
            )

        self.assertEqual(auction, {})
        self.assertEqual(close["2026-06-30"], 12_345.67)

    def test_skips_intraday_close_samples_before_the_session_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "close.json"
            self.assertIsNone(
                auction_turnover.persist_close_turnover_sample(
                    generated_at="2026-06-30 14:59:59",
                    turnover_yi=12_000,
                    quote_count=5_200,
                    path=path,
                )
            )
            self.assertFalse(path.exists())

    def test_index_close_series_fills_missing_report_days_for_opening_profile(self):
        start = dt.date(2026, 6, 1)
        auction_days = [
            (start + dt.timedelta(days=offset)).isoformat()
            for offset in range(10)
        ]
        missing_day = auction_days[-1]
        report_close_days = auction_days[:-1]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "auction.json"
            state_path.write_text(json.dumps({
                "samples": [
                    {
                        "date": day,
                        "captured_at": f"{day} 09:25:02",
                        "auction_turnover_yi": 120 if day == "2026-07-01" else 100,
                        "quote_count": 5_100,
                    }
                    for day in [*auction_days, "2026-07-01"]
                ],
            }), encoding="utf-8")
            close_path = root / "close.json"
            close_path.write_text(json.dumps({
                "samples": [
                    {
                        "date": day,
                        "captured_at": f"{day} 15:00:08",
                        "turnover_yi": 10_000,
                        "quote_count": 5_200,
                    }
                    for day in report_close_days
                ],
            }), encoding="utf-8")
            db_path = root / "reports.db"
            sqlite3.connect(db_path).close()
            profile = auction_turnover.fetch_auction_turnover_profile(
                dt.date(2026, 7, 1),
                db_path=db_path,
                state_path=state_path,
                close_state_path=close_path,
                close_series_fetcher=lambda _day: {
                    missing_day: 10_000,
                    "2026-07-01": 99_999,
                },
            )

        self.assertEqual(profile["profile_days"], 10)
        self.assertEqual(profile["historical_turnover_median_yi"], 10_000)
        self.assertNotIn("2026-07-01", [row["date"] for row in profile["daily_profiles"]])

    def test_local_close_records_win_over_index_fill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            close_path = root / "close.json"
            auction_turnover.persist_close_turnover_sample(
                generated_at="2026-06-30 15:00:08",
                turnover_yi=11_000,
                quote_count=5_200,
                path=close_path,
            )
            state_path = root / "auction.json"
            state_path.write_text(json.dumps({
                "samples": [{
                    "date": "2026-07-01",
                    "captured_at": "2026-07-01 09:25:02",
                    "auction_turnover_yi": 120,
                    "quote_count": 5_100,
                }]
                + [
                    {
                        "date": f"2026-06-{day:02d}",
                        "captured_at": f"2026-06-{day:02d} 09:25:02",
                        "auction_turnover_yi": 100,
                        "quote_count": 5_100,
                    }
                    for day in range(20, 31)
                ],
            }), encoding="utf-8")
            db_path = root / "reports.db"
            sqlite3.connect(db_path).close()
            profile = auction_turnover.fetch_auction_turnover_profile(
                dt.date(2026, 7, 1),
                db_path=db_path,
                state_path=state_path,
                close_state_path=close_path,
                close_series_fetcher=lambda _day: {
                    f"2026-06-{day:02d}": 9_000
                    for day in range(20, 31)
                },
            )

        close_by_date = {
            row["date"]: row["turnover_yi"]
            for row in profile["daily_profiles"]
        }
        self.assertEqual(close_by_date["2026-06-30"], 11_000)
        self.assertEqual(close_by_date["2026-06-29"], 9_000)


if __name__ == "__main__":
    unittest.main()
