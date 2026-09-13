import os
import math
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

BASE = "https://iss.moex.com/iss"
TIMEOUT = int(os.getenv("MOEX_TIMEOUT", "20"))
STAGE2 = int(os.getenv("STAGE2", "8"))
MAX_WORKERS = int(os.getenv("SCAN_WORKERS", "12"))
M1_DAYS = int(os.getenv("M1_DAYS", "3"))
H1_DAYS = int(os.getenv("H1_DAYS", "30"))
# Analyze a wider pool than the TOP size. The most liquid FORTS contracts can
# be newly listed/illiquid and have too little intraday history. If we analyze
# only the first 8, all futures can disappear from TOP even when deeper
# contracts have enough history. TOP itself is still limited to 3 in bot.py.
STOCK_POOL = int(os.getenv("STOCK_POOL", "12"))
FUTURES_POOL = int(os.getenv("FUTURES_POOL", "24"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "62"))
WATCH_SCORE = float(os.getenv("WATCH_SCORE", "55"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.000001"))

s = requests.Session()
s.headers.update({"User-Agent": "AA-Analitik/5.0"})


def rows(x):
    if not x:
        return []
    c = x.get("columns", [])
    return [dict(zip(c, r)) for r in x.get("data", [])]


def n(x, d=0.0):
    try:
        return float(x) if x not in (None, "") else d
    except Exception:
        return d


def get(url, p):
    r = s.get(url, params=p, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def ema(a, k):
    if len(a) < k:
        return None
    e = sum(a[:k]) / k
    q = 2 / (k + 1)
    for v in a[k:]:
        e = v * q + e * (1 - q)
    return e


def rsi(a, k=14):
    if len(a) < k + 1:
        return 50.0
    d = [a[i] - a[i - 1] for i in range(len(a) - k, len(a))]
    g = sum(max(v, 0) for v in d) / k
    l = sum(max(-v, 0) for v in d) / k
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(c, k=14):
    if len(c) < k + 1:
        return 0.0
    p = c[0]["close"]
    z = []
    for x in c[1:]:
        z.append(max(
            x["high"] - x["low"],
            abs(x["high"] - p),
            abs(x["low"] - p),
        ))
        p = x["close"]
    return sum(z[-k:]) / k


def parse_dt(v):
    if not v:
        return None
    try:
        z = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(z)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def engine_path(kind):
    if kind == "stock":
        return "engines/stock/markets/shares"
    return "engines/futures/markets/forts"


def fetch_candles(sec, kind, interval, days):
    """
    MOEX ISS officially exposes standard candle intervals such as
    1, 10, 60 and 24 minutes/days. 5 and 15 are NOT requested directly.
    For M5/M15 we fetch 1-minute candles and aggregate locally.
    """
    e = engine_path(kind)
    end = datetime.now(timezone.utc)
    start_dt = end - timedelta(days=days)
    out = []
    start = 0

    url = f"{BASE}/{e}/securities/{sec}/candles.json"

    while len(out) < 12000:
        p = {
            "iss.meta": "off",
            "interval": interval,
            "from": start_dt.date().isoformat(),
            "till": (end + timedelta(days=1)).date().isoformat(),
            "start": start,
        }

        j = get(url, p)
        block = j.get("candles", {})
        data = block.get("data", [])
        cols = block.get("columns", [])

        if not data:
            break

        for row in data:
            x = dict(zip(cols, row))
            o = n(x.get("open"), None)
            h = n(x.get("high"), None)
            lo = n(x.get("low"), None)
            c = n(x.get("close"), None)
            if None in (o, h, lo, c) or c <= 0:
                continue

            out.append({
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": n(x.get("volume")),
                "begin": x.get("begin"),
            })

        # ISS may return relatively small pages (for example 20/100 rows).
        # Do not treat a short page as the end of history; advance pagination
        # until the endpoint returns an empty page or we reach the safety cap.
        prev_start = start
        start += len(data)
        if start <= prev_start:
            break

    uniq = {}
    for x in out:
        if x.get("begin"):
            uniq[x["begin"]] = x

    return [uniq[k] for k in sorted(uniq)]


def aggregate_minutes(candles_1m, minutes):
    if minutes == 1:
        return candles_1m

    buckets = {}
    step = minutes * 60

    for c in candles_1m:
        dt = parse_dt(c.get("begin"))
        if not dt:
            continue

        ts = int(dt.timestamp())
        bucket = (ts // step) * step

        if bucket not in buckets:
            buckets[bucket] = {
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0.0),
                "begin": datetime.fromtimestamp(
                    bucket, tz=timezone.utc
                ).isoformat(),
            }
        else:
            b = buckets[bucket]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c.get("volume", 0.0)

    return [buckets[k] for k in sorted(buckets)]


def candles(sec, kind, interval):
    """Load only the minimum history needed for the indicator engine.

    H1 uses the native 60-minute endpoint. M5/M15 are built from one shared
    M1 download per instrument, so we never download the same minute history
    twice. This is the main speed-up versus the previous scanner.
    """
    if interval == 60:
        best = fetch_candles(sec, kind, 60, H1_DAYS)
        if len(best) >= 60:
            return best
        # Weekend/illiquid fallback, but keep it bounded.
        for days in (90, 365):
            data = fetch_candles(sec, kind, 60, days)
            if len(data) > len(best):
                best = data
            if len(best) >= 60:
                break
        return best

    raw = fetch_candles(sec, kind, 1, M1_DAYS)
    data = aggregate_minutes(raw, interval)
    if len(data) >= 60:
        return data

    # Only deepen history when the short window genuinely lacks enough bars.
    for days in (7, 30):
        raw = fetch_candles(sec, kind, 1, days)
        data = aggregate_minutes(raw, interval)
        if len(data) > 60:
            return data
    return data


def load_timeframes(sec, kind):
    """Fetch H1 + one shared M1 stream, then derive M15/M5 locally."""
    h = candles(sec, kind, 60)
    raw = fetch_candles(sec, kind, 1, M1_DAYS)
    m = aggregate_minutes(raw, 15)
    f = aggregate_minutes(raw, 5)

    # Deepen only if the shared short window is insufficient.
    if len(m) < 40 or len(f) < 40:
        raw = fetch_candles(sec, kind, 1, 7)
        m = aggregate_minutes(raw, 15)
        f = aggregate_minutes(raw, 5)

    return h, m, f


def snapshot(sec, kind):
    e = engine_path(kind)
    j = get(
        f"{BASE}/{e}/securities/{sec}.json",
        {
            "iss.meta": "off",
            "iss.only": "marketdata,securities",
        },
    )
    a = rows(j.get("marketdata"))
    b = rows(j.get("securities"))
    return {**(b[0] if b else {}), **(a[0] if a else {})}


def discover_stocks():
    j = get(
        f"{BASE}/engines/stock/markets/shares/boards/TQBR/securities.json",
        {
            "iss.meta": "off",
            "iss.only": "securities,marketdata",
        },
    )

    ss = {x.get("SECID"): x for x in rows(j.get("securities"))}
    mm = {x.get("SECID"): x for x in rows(j.get("marketdata"))}
    out = []

    for sec, x in ss.items():
        m = mm.get(sec, {})
        last = n(m.get("LAST"))

        if not sec or last <= 0:
            continue

        turnover = n(m.get("VALTODAY"))
        trades = n(m.get("NUMTRADES"))
        vol = n(m.get("VOLUME"))
        liq = (
            math.log1p(turnover)
            + 0.4 * math.log1p(trades)
            + 0.1 * math.log1p(vol)
        )

        out.append({
            "kind": "stock",
            "secid": sec,
            "symbol": sec,
            "name": x.get("SHORTNAME") or sec,
            "last": last,
            "liq": liq,
            "lot": max(1, n(x.get("LOTSIZE"), 1)),
        })

    return sorted(out, key=lambda x: x["liq"], reverse=True)


def discover_futures():
    j = get(
        f"{BASE}/engines/futures/markets/forts/securities.json",
        {
            "iss.meta": "off",
            "iss.only": "securities",
        },
    )

    groups = {}

    for x in rows(j.get("securities")):
        sec = x.get("SECID")
        name = str(x.get("SHORTNAME") or sec or "")

        if not sec:
            continue

        m = re.match(r"^(.+)-\d{1,2}\.\d{2}$", name)
        if not m:
            continue

        root = m.group(1)
        vol = n(x.get("VOLUME"))
        oi = n(x.get("OPENPOSITION"))
        tr = n(x.get("NUMTRADES"))
        liq = (
            math.log1p(vol)
            + 0.55 * math.log1p(oi)
            + 0.25 * math.log1p(tr)
        )

        groups.setdefault(root, []).append({
            "kind": "future",
            "secid": sec,
            "symbol": name,
            "root": root,
            "liq": liq,
            "oi": oi,
            "volume": vol,
            "lot": max(1, n(x.get("LOTSIZE"), 1)),
            "step": n(x.get("MINSTEP"), 0.01),
            "step_price": n(x.get("STEPPRICE"), 0),
        })

    return sorted(
        [max(v, key=lambda x: x["liq"]) for v in groups.values()],
        key=lambda x: x["liq"],
        reverse=True,
    )


def stage1(x):
    try:
        q = snapshot(x["secid"], x["kind"])
        z = x.copy()
        z["last"] = n(q.get("LAST"), z.get("last", 0))
        z["volume"] = n(q.get("VOLUME"), z.get("volume", 0))
        z["oi"] = n(q.get("OPENPOSITION"), z.get("oi", 0))
        z["turnover"] = n(q.get("VALTODAY"), 0)
        z["trades"] = n(q.get("NUMTRADES"), 0)
        z["step_price"] = n(
            q.get("STEPPRICE"),
            z.get("step_price", 0),
        )
        z["step"] = n(q.get("MINSTEP"), z.get("step", 0.01))
        z["lot"] = max(
            1,
            n(q.get("LOTSIZE"), z.get("lot", 1)),
        )
        return z
    except Exception as e:
        z = x.copy()
        z["error"] = str(e)
        return z


def analyze(x):
    try:
        h, m, f = load_timeframes(x["secid"], x["kind"])

        if min(len(h), len(m), len(f)) < 40:
            return {
                **x,
                "status": "WAIT",
                "score": 0,
                "side": "WAIT",
                "watch": False,
                "reason": (
                    f"Недостаточно свечей "
                    f"(H1={len(h)}, M15={len(m)}, M5={len(f)})"
                ),
            }

        q = snapshot(x["secid"], x["kind"])
        last_quote = n(q.get("LAST"), 0)
        price = last_quote if last_quote > MIN_PRICE else f[-1]["close"]
        if price <= MIN_PRICE:
            return {
                **x, "status": "WAIT", "score": 0, "side": "WAIT",
                "reason": "Невалидная цена: нет актуальной котировки и закрытия свечи",
            }

        H = [z["close"] for z in h]
        M = [z["close"] for z in m]
        F = [z["close"] for z in f]

        h20, h50 = ema(H, 20), ema(H, 50)
        m20, m50 = ema(M, 20), ema(M, 50)
        f20, f50 = ema(F, 20), ema(F, 50)

        hs = (
            "UP" if h20 and h50 and h20 > h50
            else "DOWN" if h20 and h50 and h20 < h50
            else "FLAT"
        )
        ms = (
            "UP" if m20 and m50 and m20 > m50
            else "DOWN" if m20 and m50 and m20 < m50
            else "FLAT"
        )
        fs = (
            "UP" if f20 and f50 and f20 > f50
            else "DOWN" if f20 and f50 and f20 < f50
            else "FLAT"
        )

        a = max(
            atr(m),
            price * 0.003,
            x.get("step", 0.01) * 5,
        )

        sup = min(z["low"] for z in m[-21:-1])
        res = max(z["high"] for z in m[-21:-1])
        hi = max(z["high"] for z in f[-13:-1])
        lo = min(z["low"] for z in f[-13:-1])

        avg = sum(z["volume"] for z in f[-21:-1]) / 20
        vr = f[-1]["volume"] / avg if avg else 1
        R = rsi(M)

        def score(side):
            v = 0

            if side == "LONG":
                v += 25 if hs == "UP" else -15 if hs == "DOWN" else 0
                v += 15 if ms == "UP" else 0
                v += 10 if fs == "UP" else 0
                v += 10 if price > m20 else 0
                v += 5 if R < 72 else -7 if R > 78 else 0
            else:
                v += 25 if hs == "DOWN" else -15 if hs == "UP" else 0
                v += 15 if ms == "DOWN" else 0
                v += 10 if fs == "DOWN" else 0
                v += 10 if price < m20 else 0
                v += 5 if R > 28 else -7 if R < 22 else 0

            v += 7 if vr >= 1.15 else 0
            v += 4 if x["kind"] == "future" and x.get("oi", 0) > 0 else 0

            return max(0, min(100, v))

        sl_score = score("LONG")
        ss_score = score("SHORT")
        side = "LONG" if sl_score >= ss_score else "SHORT"
        sc = max(sl_score, ss_score)

        trigger = hi if side == "LONG" else lo

        crossed = (
            any(z["close"] > trigger for z in f[-10:-1])
            if side == "LONG"
            else any(z["close"] < trigger for z in f[-10:-1])
        )

        retest = (
            any(
                z["low"] <= trigger + a * 0.15
                and z["close"] > trigger
                for z in f[-4:]
            )
            if side == "LONG"
            else any(
                z["high"] >= trigger - a * 0.15
                and z["close"] < trigger
                for z in f[-4:]
            )
        )

        hard = []

        if not (crossed and retest):
            hard.append("нет breakout+retest M5")

        if side == "LONG" and hs != "UP":
            hard.append("H1 не подтверждает LONG")

        if side == "SHORT" and hs != "DOWN":
            hard.append("H1 не подтверждает SHORT")

        mid_low = sup + (res - sup) * 0.30
        mid_high = sup + (res - sup) * 0.70

        if mid_low < price < mid_high:
            hard.append("середина диапазона")

        entry = trigger

        if side == "LONG":
            slv = min(sup, entry - a * 0.45)
            risk = max(entry - slv, 0.000001)
            t1 = entry + risk * 1.5
            t2 = entry + risk * 2.2
            t3 = entry + risk * 3
        else:
            slv = max(res, entry + a * 0.45)
            risk = max(slv - entry, 0.000001)
            t1 = entry - risk * 1.5
            t2 = entry - risk * 2.2
            t3 = entry - risk * 3

        if sc >= MIN_SCORE and not hard:
            status = side
        else:
            status = "WAIT"

        return {
            **x,
            "status": status,
            "score": round(sc, 1),
            "long_score": round(sl_score, 1),
            "short_score": round(ss_score, 1),
            "side": side,
            "price": price,
            "regime": (
                "TREND UP" if hs == "UP"
                else "TREND DOWN" if hs == "DOWN"
                else "RANGE"
            ),
            "h1": hs,
            "m15": ms,
            "m5": fs,
            "data_asof": f[-1].get("begin") or m[-1].get("begin") or h[-1].get("begin"),
            "rsi": round(R, 1),
            "support": sup,
            "resistance": res,
            "trigger": trigger,
            "entry": entry,
            "sl": slv,
            "tp1": t1,
            "tp2": t2,
            "tp3": t3,
            "rr": 3,
            "volume_ratio": round(vr, 2),
            "liquidity": "OK" if x.get("liq", 0) > 10 else "CAUTION",
            "watch": sc >= WATCH_SCORE and bool(hard),
            "trigger_condition": (
                f"LONG: пробой и ретест {trigger:.6g}" if side == "LONG"
                else f"SHORT: пробой и ретест {trigger:.6g}"
            ),
            "reason": (
                "; ".join(hard)
                if hard
                else "подтверждённый сетап"
            ),
        }

    except Exception as e:
        return {
            **x,
            "status": "WAIT",
            "score": 0,
            "side": "WAIT",
            "watch": False,
            "reason": "Ошибка: " + str(e),
        }


def run_scan():
    # Discovery endpoints already provide the liquidity ranking.
    # Do NOT snapshot every security here: 400+ sequential ISS requests
    # were making /scan take several minutes. Only the final candidates
    # need detailed snapshots/candles in analyze().
    stocks = discover_stocks()
    futures = discover_futures()

    # STAGE2 controls TOP size/quality, but the analysis pool must be wider.
    # This prevents technically unusable front contracts from consuming the
    # entire futures quota.
    candidates = (
        stocks[:STOCK_POOL]
        + futures[:FUTURES_POOL]
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        result = list(ex.map(analyze, candidates))

    all_result = result
    asofs = [x.get("data_asof") for x in all_result if x.get("data_asof")]
    # The bot is primarily a research/reporting service. Outside the MOEX
    # trading window we label the report historical so the last candle is
    # never presented as a live quote. Moscow time is UTC+3 in this setup.
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    weekday = now_msk.weekday()
    minutes = now_msk.hour * 60 + now_msk.minute
    market_open = (weekday < 5 and 10*60 <= minutes <= 23*60+50)
    market_mode = "LIVE_OR_LATEST" if market_open else "HISTORICAL"

    return {
        "longs": sorted(
            [x for x in result if x["status"] == "LONG"],
            key=lambda x: x["score"],
            reverse=True,
        ),
        "shorts": sorted(
            [x for x in result if x["status"] == "SHORT"],
            key=lambda x: x["score"],
            reverse=True,
        ),
        "watch": sorted(
            [x for x in result if x["status"] == "WAIT"],
            key=lambda x: x["score"],
            reverse=True,
        ),
        "stocks": [
            x for x in result if x["kind"] == "stock"
        ],
        "futures": [
            x for x in result if x["kind"] == "future"
        ],
        "meta": {
            "stocks": len(stocks),
            "futures": len(futures),
            "stage2": STAGE2,
            "stock_pool": STOCK_POOL,
            "futures_pool": FUTURES_POOL,
            "workers": MAX_WORKERS,
            "m1_days": M1_DAYS,
            "h1_days": H1_DAYS,
            "generated": datetime.now(timezone.utc).isoformat(),
            "market_mode": market_mode,
            "analysis_asof": max(asofs) if asofs else None,
            "history_mode": True,
        },
    }
