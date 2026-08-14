#!/usr/bin/env python3
import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from app.market_data.data_source_proxy import data_source_urlopen
except ImportError:  # pragma: no cover - legacy top-level import path
    from market_data.data_source_proxy import data_source_urlopen

EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def http_get_json(url, params, retries=3, sleep_seconds=1.2):
    full = url + "?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
        "Connection": "close",
    }
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(full, headers=headers)
            with data_source_urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)
    raise last_error


def normalize_symbol(raw):
    s = raw.strip().lower()
    if s.startswith(("sh", "sz", "bj")) and len(s) >= 8:
        market = s[:2]
        code = s[2:]
    elif s.isdigit() and len(s) == 6:
        code = s
        if code.startswith(("6", "9")):
            market = "sh"
        elif code.startswith(("4", "8")):
            market = "bj"
        else:
            market = "sz"
    else:
        raise ValueError(f"无法识别股票代码: {raw}")

    if market == "sh":
        secid = f"1.{code}"
    elif market == "sz":
        secid = f"0.{code}"
    elif market == "bj":
        secid = f"0.{code}"
    else:
        raise ValueError(f"不支持市场: {market}")
    return {"raw": raw, "market": market, "code": code, "secid": secid, "display": market + code}


def get_quote(symbol):
    sym = normalize_symbol(symbol)
    try:
        data = http_get_json(EASTMONEY_QUOTE, {
            "secid": sym["secid"],
            "fields": "f43,f57,f58,f60,f169,f170,f171,f168,f46,f44,f45,f47,f48,f50"
        }).get("data")
        if not data:
            raise RuntimeError("未获取到行情数据")

        def price(v):
            if v is None:
                return None
            return round(float(v) / 100, 2)

        def pct(v):
            if v is None:
                return None
            return round(float(v) / 100, 2)

        def amount(v):
            if v is None:
                return None
            return float(v)

        return {
            "symbol": sym["display"],
            "code": sym["code"],
            "name": data.get("f58"),
            "price": price(data.get("f43")),
            "open": price(data.get("f46")),
            "high": price(data.get("f44")),
            "low": price(data.get("f45")),
            "prev_close": price(data.get("f60")),
            "change": price(data.get("f169")),
            "change_pct": pct(data.get("f170")),
            "amplitude_pct": pct(data.get("f171")),
            "volume_lots": amount(data.get("f47")),
            "turnover_yuan": amount(data.get("f48")),
            "volume_ratio": data.get("f50"),
            "source": "Eastmoney push2 quote"
        }
    except Exception:
        req = urllib.request.Request(TENCENT_QUOTE + sym["display"], headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
        with data_source_urlopen(req, timeout=20) as resp:
            text = resp.read().decode("gbk", "ignore")
        parts = text.split('="', 1)[-1].rstrip('";').split('~')
        if len(parts) < 38:
            raise RuntimeError("未获取到腾讯行情数据")
        price = float(parts[3])
        prev_close = float(parts[4])
        high = float(parts[33])
        low = float(parts[34])
        open_price = float(parts[5])
        change = round(price - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else None
        amplitude_pct = round(((high - low) / prev_close) * 100, 2) if prev_close else None
        return {
            "symbol": sym["display"],
            "code": sym["code"],
            "name": parts[1],
            "price": price,
            "open": open_price,
            "high": high,
            "low": low,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "amplitude_pct": amplitude_pct,
            "volume_lots": float(parts[6]),
            "turnover_yuan": float(parts[37]) * 10000,
            "volume_ratio": None,
            "source": "Tencent qt quote fallback"
        }


def get_klines(symbol, count=120):
    sym = normalize_symbol(symbol)
    key = sym["display"]
    resp = http_get_json(TENCENT_KLINE, {
        "param": f"{key},day,,,{count},qfq"
    })
    data = (resp.get("data") or {}).get(key) or {}
    klines = data.get("qfqday") or data.get("day") or []
    if not klines:
        raise RuntimeError("未获取到K线数据")
    rows = []
    for p in klines:
        # [date, open, close, high, low, volume]
        rows.append({
            "date": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "volume": float(p[5]),
        })
    return rows


def moving_avg(values, n):
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = sum(values[i - n + 1:i + 1]) / n
    return out


def compute_bbi(rows):
    closes = [r["close"] for r in rows]
    ma3 = moving_avg(closes, 3)
    ma6 = moving_avg(closes, 6)
    ma12 = moving_avg(closes, 12)
    ma24 = moving_avg(closes, 24)
    out = []
    for i in range(len(rows)):
        vals = [ma3[i], ma6[i], ma12[i], ma24[i]]
        out.append(None if any(v is None for v in vals) else sum(vals) / 4)
    return out


def safe_round(v, n=2):
    return None if v is None else round(v, n)


def analyze_b1(symbol):
    rows = get_klines(symbol, 120)
    quote = get_quote(symbol)
    bbi = compute_bbi(rows)
    for r, b in zip(rows, bbi):
        r["bbi"] = b
        r["above_bbi"] = None if b is None else r["close"] >= b
        r["dist_bbi_pct"] = None if b is None else (r["close"] / b - 1) * 100
        r["change_pct"] = None

    for i in range(1, len(rows)):
        prev_close = rows[i-1]["close"]
        rows[i]["change_pct"] = (rows[i]["close"] / prev_close - 1) * 100 if prev_close else None

    recent = rows[-1]
    prev = rows[-2]
    prev2 = rows[-3]
    bbi_recent = recent["bbi"]
    bbi_prev = prev["bbi"]
    bbi_prev2 = prev2["bbi"]

    if bbi_recent is None or bbi_prev is None or bbi_prev2 is None:
        raise RuntimeError("K线数量不足，无法计算BBI")

    low20 = min(r["low"] for r in rows[-20:])
    high40 = max(r["high"] for r in rows[-40:])
    high60 = max(r["high"] for r in rows[-60:])
    recent3_low = min(r["low"] for r in rows[-3:])
    recent3_vol = statistics.mean(r["volume"] for r in rows[-3:])
    prior5_vol = statistics.mean(r["volume"] for r in rows[-8:-3])

    conditions = {
        "trend_established": (high40 / low20 - 1) >= 0.12,
        "bbi_upward": bbi_recent > bbi_prev > bbi_prev2,
        "price_above_bbi": recent["close"] >= bbi_recent,
        "recent_pullback_near_bbi": abs((recent3_low / bbi_recent - 1) * 100) <= 3.0,
        "pullback_not_broken": recent3_low >= bbi_recent * 0.97,
        "pullback_volume_contracted": recent3_vol <= prior5_vol * 0.95 if prior5_vol > 0 else False,
        "re_strength_today": recent["close"] > prev["high"] or (recent["close"] > bbi_recent and (recent["change_pct"] or 0) > 0),
        "not_far_from_bbi": ((recent["close"] / bbi_recent - 1) * 100) <= 8.0,
        "not_breaking_down": recent["close"] >= recent3_low,
    }
    passed = [k for k, v in conditions.items() if v]
    score = len(passed)
    if score >= 8:
        verdict = "高匹配回踩观察"
    elif score >= 6:
        verdict = "中等匹配回踩观察"
    elif score >= 4:
        verdict = "弱匹配回踩观察"
    else:
        verdict = "不匹配回踩观察"

    risk_flags = []
    dist = ((recent["close"] / bbi_recent - 1) * 100)
    if recent["close"] < bbi_recent:
        risk_flags.append("收盘在BBI下方")
    if dist > 8:
        risk_flags.append("价格偏离BBI过远，容易变成追高")
    if (recent["change_pct"] or 0) < -3:
        risk_flags.append("当日走弱较明显")
    if recent["high"] >= high60 * 0.98:
        risk_flags.append("接近60日新高，注意不要把回踩观察做成高潮追涨")

    return {
        "quote": quote,
        "analysis_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bbi": {
            "today": safe_round(bbi_recent, 2),
            "yesterday": safe_round(bbi_prev, 2),
            "two_days_ago": safe_round(bbi_prev2, 2),
            "distance_pct": safe_round(dist, 2),
        },
        "recent_k": {
            "today": {k: safe_round(v, 2) if isinstance(v, float) else v for k, v in recent.items()},
            "yesterday": {k: safe_round(v, 2) if isinstance(v, float) else v for k, v in prev.items()},
            "two_days_ago": {k: safe_round(v, 2) if isinstance(v, float) else v for k, v in prev2.items()},
        },
        "conditions": conditions,
        "score": score,
        "score_total": len(conditions),
        "verdict": verdict,
        "risk_flags": risk_flags,
        "notes": [
            "这是基于BBI/趋势回踩的启发式观察，不作为当前6战法里的买入战法。",
            "更适合强势股右侧回踩再转强的场景，不适合长期阴跌票。",
            "请结合板块热度、题材强弱、市场情绪和个人风控。"
        ]
    }


def get_klines_array(symbol, count=60):
    """Return K-line data for downstream BBI/ATR computation."""
    rows = get_klines(symbol, count=count)
    closes = [r["close"] for r in rows] if rows else []
    return {"symbol": symbol, "closes": closes, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description="CN stock helper for real-time quote and pullback heuristic analysis")
    sub = parser.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("quote", help="查询实时股价")
    q.add_argument("symbol", help="股票代码，如 600519 / sh600519 / 000001")

    b = sub.add_parser("b1", help="做回踩启发式观察")
    b.add_argument("symbol", help="股票代码，如 600519 / sh600519 / 000001")

    s = sub.add_parser("screen", help="批量做回踩启发式筛选")
    s.add_argument("symbols", nargs="+", help="多个股票代码，空格分隔")

    k = sub.add_parser("kline", help="获取日K线 closes 数组（用于BBI计算）")
    k.add_argument("symbol", help="股票代码，如 600519 / 000001")
    k.add_argument("count", nargs="?", type=int, default=60, help="K线数量，默认60")

    args = parser.parse_args()
    if args.cmd == "quote":
        result = get_quote(args.symbol)
    elif args.cmd == "b1":
        result = analyze_b1(args.symbol)
    elif args.cmd == "screen":
        result = []
        for symbol in args.symbols:
            try:
                item = analyze_b1(symbol)
                result.append({
                    "symbol": item["quote"]["symbol"],
                    "name": item["quote"]["name"],
                    "price": item["quote"]["price"],
                    "bbi": item["bbi"]["today"],
                    "distance_pct": item["bbi"]["distance_pct"],
                    "score": item["score"],
                    "score_total": item["score_total"],
                    "verdict": item["verdict"],
                    "risk_flags": item["risk_flags"],
                    "source": item["quote"]["source"],
                })
            except Exception as e:
                result.append({"symbol": symbol, "error": str(e)})
        result.sort(key=lambda x: (x.get("score", -1), x.get("distance_pct", -999)), reverse=True)
    elif args.cmd == "kline":
        result = get_klines_array(args.symbol, args.count)
    else:
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
