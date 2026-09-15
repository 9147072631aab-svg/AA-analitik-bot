from collections import Counter
import os
import math
import re
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

BASE = "https://iss.moex.com/iss"
TIMEOUT = int(os.getenv("MOEX_TIMEOUT", "15"))
MAX_WORKERS = int(os.getenv("SCAN_WORKERS", "6"))
STOCK_POOL = int(os.getenv("STOCK_POOL", "24"))
FUTURES_POOL = int(os.getenv("FUTURES_POOL", "24"))

MIN_SCORE = float(os.getenv("MIN_SCORE", "62"))
WATCH_SCORE = float(os.getenv("WATCH_SCORE", "60"))
MAX_TRIGGER_ATR = float(os.getenv("MAX_TRIGGER_ATR", "1.50"))
MAX_WATCH_TRIGGER_ATR = float(os.getenv("MAX_WATCH_TRIGGER_ATR", "1.75"))

MIN_PRICE = float(os.getenv("MIN_PRICE", "0.000001"))
H1_DAYS = int(os.getenv("H1_DAYS", "10"))
M1_DAYS = int(os.getenv("M1_DAYS", "3"))

ADX_MIN = float(os.getenv("ADX_MIN", "18"))
VP_BINS = int(os.getenv("VP_BINS", "24"))
VP_LOOKBACK = int(os.getenv("VP_LOOKBACK", "96"))
SR_LOOKBACK = int(os.getenv("SR_LOOKBACK", "48"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "12"))
RETEST_BARS = int(os.getenv("RETEST_BARS", "4"))

MSK = ZoneInfo("Europe/Moscow")
_thread_local = threading.local()
log = logging.getLogger("aa_scanner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def session():
    if not hasattr(_thread_local, "s"):
        _thread_local.s = requests.Session()
        _thread_local.s.headers.update({"User-Agent": "AA-Analitik/Protocol-v1.3"})
    return _thread_local.s


def rows(block):
    if not block:
        return []
    cols = block.get("columns", [])
    return [dict(zip(cols, row)) for row in block.get("data", [])]


def n(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except Exception:
        return default


def get(url, params):
    last = None
    for attempt in range(3):
        try:
            response = session().get(url, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise last


def parse_dt(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def msk_dt(value):
    dt = parse_dt(value)
    return dt.astimezone(MSK) if dt else None


def iso_msk(value):
    dt = msk_dt(value)
    return dt.isoformat() if dt else None


def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(candles):
    if not candles:
        return []
    result = []
    previous_close = candles[0]["close"]
    for candle in candles[1:]:
        result.append(
            max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
        )
        previous_close = candle["close"]
    return result


def atr(candles, period=14):
    trs = true_ranges(candles)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def adx_di(candles, period=14):
    if len(candles) < period * 2 + 2:
        return 0.0, 0.0, 0.0

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        trs.append(
            max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
        )
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    tr = sum(trs[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])

    dx_values = []
    plus_di = 0.0
    minus_di = 0.0

    for i in range(period, len(trs)):
        tr = tr - tr / period + trs[i]
        plus = plus - plus / period + plus_dm[i]
        minus = minus - minus / period + minus_dm[i]

        plus_di = 100.0 * plus / tr if tr else 0.0
        minus_di = 100.0 * minus / tr if tr else 0.0
        denom = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denom if denom else 0.0)

    if len(dx_values) < period:
        return 0.0, plus_di, minus_di

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    return adx, plus_di, minus_di


def engine_path(kind):
    return (
        "engines/stock/markets/shares"
        if kind == "stock"
        else "engines/futures/markets/forts"
    )


def fetch_candles(secid, kind, interval, days):
    endpoint = f"{BASE}/{engine_path(kind)}/securities/{secid}/candles.json"
    end = datetime.now(timezone.utc)
    start_dt = end - timedelta(days=days)
    result = []
    offset = 0

    while len(result) < 12000:
        params = {
            "iss.meta": "off",
            "interval": interval,
            "from": start_dt.date().isoformat(),
            "till": (end + timedelta(days=1)).date().isoformat(),
            "start": offset,
        }
        payload = get(endpoint, params)
        block = payload.get("candles", {})
        data = block.get("data", [])
        cols = block.get("columns", [])

        if not data:
            break

        for raw in data:
            row = dict(zip(cols, raw))
            open_price = n(row.get("open"), None)
            high = n(row.get("high"), None)
            low = n(row.get("low"), None)
            close = n(row.get("close"), None)

            if None in (open_price, high, low, close) or close <= 0:
                continue

            begin = iso_msk(row.get("begin"))
            if not begin:
                continue

            result.append(
                {
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": n(row.get("volume")),
                    "begin": begin,
                }
            )

        previous = offset
        offset += len(data)
        if offset <= previous:
            break

    unique = {x["begin"]: x for x in result}
    return [unique[key] for key in sorted(unique)]


def aggregate_minutes(candles_1m, minutes):
    if minutes == 1:
        return candles_1m

    buckets = {}
    step = minutes * 60

    for candle in candles_1m:
        dt = parse_dt(candle.get("begin"))
        if not dt:
            continue

        ts = int(dt.timestamp())
        bucket = (ts // step) * step

        if bucket not in buckets:
            buckets[bucket] = {
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle.get("volume", 0.0),
                "begin": datetime.fromtimestamp(
                    bucket, tz=timezone.utc
                ).astimezone(MSK).isoformat(),
            }
        else:
            item = buckets[bucket]
            item["high"] = max(item["high"], candle["high"])
            item["low"] = min(item["low"], candle["low"])
            item["close"] = candle["close"]
            item["volume"] += candle.get("volume", 0.0)

    return [buckets[key] for key in sorted(buckets)]


def load_timeframes(secid, kind):
    h1 = fetch_candles(secid, kind, 60, H1_DAYS)
    if len(h1) < 60:
        for days in (30, 90):
            deeper = fetch_candles(secid, kind, 60, days)
            if len(deeper) > len(h1):
                h1 = deeper
            if len(h1) >= 60:
                break

    raw = fetch_candles(secid, kind, 1, M1_DAYS)
    m15 = aggregate_minutes(raw, 15)
    m5 = aggregate_minutes(raw, 5)

    if len(m15) < 40 or len(m5) < 40:
        for days in (7, 20):
            deeper = fetch_candles(secid, kind, 1, days)
            m15_deeper = aggregate_minutes(deeper, 15)
            m5_deeper = aggregate_minutes(deeper, 5)
            if len(m15_deeper) > len(m15):
                m15 = m15_deeper
            if len(m5_deeper) > len(m5):
                m5 = m5_deeper
            if len(m15) >= 40 and len(m5) >= 40:
                break

    return h1, m15, m5


def snapshot(secid, kind):
    endpoint = f"{BASE}/{engine_path(kind)}/securities/{secid}.json"
    payload = get(
        endpoint,
        {
            "iss.meta": "off",
            "iss.only": "marketdata,securities",
        },
    )
    market = rows(payload.get("marketdata"))
    security = rows(payload.get("securities"))
    return {**(security[0] if security else {}), **(market[0] if market else {})}


def discover_stocks():
    payload = get(
        f"{BASE}/engines/stock/markets/shares/boards/TQBR/securities.json",
        {
            "iss.meta": "off",
            "iss.only": "securities,marketdata",
        },
    )

    securities = {x.get("SECID"): x for x in rows(payload.get("securities"))}
    market = {x.get("SECID"): x for x in rows(payload.get("marketdata"))}
    result = []

    for secid, security in securities.items():
        quote = market.get(secid, {})
        last = n(quote.get("LAST"))

        if not secid or last <= 0:
            continue

        turnover = n(quote.get("VALTODAY"))
        trades = n(quote.get("NUMTRADES"))
        volume = n(quote.get("VOLUME"))

        liquidity = (
            math.log1p(turnover)
            + 0.40 * math.log1p(trades)
            + 0.10 * math.log1p(volume)
        )

        result.append(
            {
                "kind": "stock",
                "secid": secid,
                "symbol": secid,
                "name": security.get("SHORTNAME") or secid,
                "last": last,
                "liq": liquidity,
                "lot": max(1, n(security.get("LOTSIZE"), 1)),
            }
        )

    return sorted(result, key=lambda x: x["liq"], reverse=True)


def discover_futures():
    payload = get(
        f"{BASE}/engines/futures/markets/forts/securities.json",
        {
            "iss.meta": "off",
            "iss.only": "securities",
        },
    )

    groups = {}

    for security in rows(payload.get("securities")):
        secid = security.get("SECID")
        name = str(security.get("SHORTNAME") or secid or "")

        if not secid:
            continue

        match = re.match(r"^(.+)-\d{1,2}\.\d{2}$", name)
        if not match:
            continue

        root = match.group(1)
        volume = n(security.get("VOLUME"))
        oi = n(security.get("OPENPOSITION"))
        trades = n(security.get("NUMTRADES"))

        liquidity = (
            math.log1p(volume)
            + 0.55 * math.log1p(oi)
            + 0.25 * math.log1p(trades)
        )

        groups.setdefault(root, []).append(
            {
                "kind": "future",
                "secid": secid,
                "symbol": name,
                "root": root,
                "liq": liquidity,
                "oi": oi,
                "volume": volume,
                "lot": max(1, n(security.get("LOTSIZE"), 1)),
                "step": n(security.get("MINSTEP"), 0.01),
                "step_price": n(security.get("STEPPRICE"), 0),
            }
        )

    return sorted(
        [max(items, key=lambda x: x["liq"]) for items in groups.values()],
        key=lambda x: x["liq"],
        reverse=True,
    )


def session_vwap(candles):
    if not candles:
        return 0.0

    last_dt = parse_dt(candles[-1].get("begin"))
    if not last_dt:
        return 0.0

    target_date = last_dt.astimezone(MSK).date()
    pv = 0.0
    volume = 0.0

    for candle in candles:
        dt = parse_dt(candle.get("begin"))
        if not dt or dt.astimezone(MSK).date() != target_date:
            continue

        typical = (
            candle["high"] + candle["low"] + candle["close"]
        ) / 3.0
        vol = max(candle.get("volume", 0.0), 0.0)

        if vol <= 0:
            continue

        pv += typical * vol
        volume += vol

    return pv / volume if volume else candles[-1]["close"]


def volume_profile(candles, bins=VP_BINS, lookback=VP_LOOKBACK):
    sample = candles[-lookback:] if len(candles) > lookback else candles[:]

    if not sample:
        return {
            "poc": 0.0,
            "vah": 0.0,
            "val": 0.0,
            "hvn": [],
            "lvn": [],
            "bins": [],
        }

    low = min(x["low"] for x in sample)
    high = max(x["high"] for x in sample)

    if high <= low:
        return {
            "poc": low,
            "vah": high,
            "val": low,
            "hvn": [low],
            "lvn": [],
            "bins": [(low, high, sum(x.get("volume", 0.0) for x in sample))],
        }

    width = (high - low) / bins
    profile = [0.0] * bins

    for candle in sample:
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        idx = int((typical - low) / width)
        idx = max(0, min(bins - 1, idx))
        profile[idx] += max(candle.get("volume", 0.0), 0.0)

    total = sum(profile)
    if total <= 0:
        poc_idx = min(
            bins - 1,
            max(0, int((sample[-1]["close"] - low) / width)),
        )
        return {
            "poc": low + (poc_idx + 0.5) * width,
            "vah": high,
            "val": low,
            "hvn": [],
            "lvn": [],
            "bins": [],
        }

    poc_idx = max(range(bins), key=lambda i: profile[i])

    # Value area: expand around POC until approximately 70% of volume is included.
    target = total * 0.70
    included = profile[poc_idx]
    left = poc_idx
    right = poc_idx

    while included < target and (left > 0 or right < bins - 1):
        left_vol = profile[left - 1] if left > 0 else -1.0
        right_vol = profile[right + 1] if right < bins - 1 else -1.0

        if right_vol >= left_vol and right < bins - 1:
            right += 1
            included += profile[right]
        elif left > 0:
            left -= 1
            included += profile[left]
        else:
            break

    poc = low + (poc_idx + 0.5) * width
    val = low + left * width
    vah = low + (right + 1) * width

    mean_vol = total / bins
    hvn = []
    lvn = []

    for i, vol in enumerate(profile):
        center = low + (i + 0.5) * width
        if vol >= mean_vol * 1.50:
            hvn.append(center)
        elif vol <= mean_vol * 0.35:
            lvn.append(center)

    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "hvn": hvn,
        "lvn": lvn,
        "bins": [
            (
                low + i * width,
                low + (i + 1) * width,
                profile[i],
            )
            for i in range(bins)
        ],
    }


def support_resistance(m15):
    sample = m15[-(SR_LOOKBACK + 1):-1]
    if not sample:
        return 0.0, 0.0

    support = min(x["low"] for x in sample)
    resistance = max(x["high"] for x in sample)
    return support, resistance


def breakout_retest(m5, side, trigger, atr_value):
    closed = m5[:-1]
    if len(closed) < BREAKOUT_LOOKBACK + RETEST_BARS + 2:
        return False, False, None

    lookback = closed[-(BREAKOUT_LOOKBACK + RETEST_BARS):-RETEST_BARS]
    retest = closed[-RETEST_BARS:]

    if not lookback or not retest:
        return False, False, None

    crossed = any(
        c["close"] > trigger
        for c in lookback
    ) if side == "LONG" else any(
        c["close"] < trigger
        for c in lookback
    )

    tolerance = max(atr_value * 0.15, MIN_PRICE)

    if side == "LONG":
        confirmed = any(
            c["low"] <= trigger + tolerance
            and c["close"] > trigger
            for c in retest
        )
    else:
        confirmed = any(
            c["high"] >= trigger - tolerance
            and c["close"] < trigger
            for c in retest
        )

    last_confirmation = retest[-1]["begin"] if confirmed else None
    return crossed, confirmed, last_confirmation


def nearest_above(levels, price):
    values = [x for x in levels if x > price]
    return min(values) if values else None


def nearest_below(levels, price):
    values = [x for x in levels if x < price]
    return max(values) if values else None


def structural_levels(side, price, atr_value, support, resistance, vp):
    all_above = [
        x for x in [resistance, vp.get("vah"), vp.get("poc")] if x > price
    ]
    all_below = [
        x for x in [support, vp.get("val"), vp.get("poc")] if x < price
    ]

    if side == "LONG":
        stop_candidates = [support]
        if vp.get("val", 0) < price:
            stop_candidates.append(vp["val"])

        stop_base = max(stop_candidates)
        sl = stop_base - max(atr_value * 0.25, MIN_PRICE)

        targets = sorted(set(all_above))
        return sl, targets

    stop_candidates = [resistance]
    if vp.get("vah", 0) > price:
        stop_candidates.append(vp["vah"])

    stop_base = min(stop_candidates)
    sl = stop_base + max(atr_value * 0.25, MIN_PRICE)

    targets = sorted(set(all_below), reverse=True)
    return sl, targets


def build_targets(side, entry, sl, atr_value, support, resistance, vp):
    risk = abs(entry - sl)

    if risk <= 0:
        return None

    context_levels = [
        support,
        resistance,
        vp.get("poc"),
        vp.get("vah"),
        vp.get("val"),
    ]

    if side == "LONG":
        candidates = sorted(
            set(
                x for x in context_levels
                if x is not None and x > entry + risk * 1.5
            )
        )
        candidates = candidates[:3]

        tp1 = candidates[0] if candidates else entry + risk * 1.8
        tp2 = candidates[1] if len(candidates) > 1 else entry + risk * 2.4
        tp3 = candidates[2] if len(candidates) > 2 else entry + risk * 3.2

    else:
        candidates = sorted(
            set(
                x for x in context_levels
                if x is not None and x < entry - risk * 1.5
            ),
            reverse=True,
        )
        candidates = candidates[:3]

        tp1 = candidates[0] if candidates else entry - risk * 1.8
        tp2 = candidates[1] if len(candidates) > 1 else entry - risk * 2.4
        tp3 = candidates[2] if len(candidates) > 2 else entry - risk * 3.2

    rr = abs(tp3 - entry) / risk

    if rr < 2.0:
        return None

    return {
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": rr,
        "risk_distance": risk,
    }


def analyze(x):
    started = time.monotonic()

    try:
        secid = x["secid"]
        kind = x["kind"]

        log.info("ANALYZE START %s %s", kind, x.get("symbol"))

        h1, m15, m5 = load_timeframes(secid, kind)

        if min(len(h1), len(m15), len(m5)) < 40:
            return {
                **x,
                "status": "WAIT",
                "side": "WAIT",
                "score": 0,
                "watch": False,
                "reason": (
                    f"Недостаточно истории "
                    f"(H1={len(h1)}, M15={len(m15)}, M5={len(m5)})"
                ),
                "duration_sec": round(time.monotonic() - started, 2),
            }

        price = m5[-1]["close"]
        if price <= MIN_PRICE:
            return {
                **x,
                "status": "WAIT",
                "side": "WAIT",
                "score": 0,
                "watch": False,
                "reason": "Невалидная цена",
            }

        h1_close = [c["close"] for c in h1]
        m15_close = [c["close"] for c in m15]
        m5_close = [c["close"] for c in m5]

        h1_20, h1_50 = ema(h1_close, 20), ema(h1_close, 50)
        m15_20, m15_50 = ema(m15_close, 20), ema(m15_close, 50)
        m5_20, m5_50 = ema(m5_close, 20), ema(m5_close, 50)

        h1_state = (
            "UP" if h1_20 and h1_50 and h1_20 > h1_50
            else "DOWN" if h1_20 and h1_50 and h1_20 < h1_50
            else "FLAT"
        )
        m15_state = (
            "UP" if m15_20 and m15_50 and m15_20 > m15_50
            else "DOWN" if m15_20 and m15_50 and m15_20 < m15_50
            else "FLAT"
        )
        m5_state = (
            "UP" if m5_20 and m5_50 and m5_20 > m5_50
            else "DOWN" if m5_20 and m5_50 and m5_20 < m5_50
            else "FLAT"
        )

        atr_m15 = max(
            atr(m15),
            price * 0.001,
            n(x.get("step"), 0.01) * 5,
        )
        atr_m5 = max(atr(m5), atr_m15 * 0.35)

        adx, plus_di, minus_di = adx_di(m15)
        vw = session_vwap(m5)
        vp = volume_profile(m5)
        support, resistance = support_resistance(m15)

        closed_m5 = m5[:-1]
        if len(closed_m5) < 20:
            raise ValueError("Недостаточно закрытых M5 свечей")

        trigger_long = max(
            c["high"] for c in closed_m5[-BREAKOUT_LOOKBACK:]
        )
        trigger_short = min(
            c["low"] for c in closed_m5[-BREAKOUT_LOOKBACK:]
        )

        avg_volume = (
            sum(c.get("volume", 0.0) for c in closed_m5[-21:-1]) / 20
            if len(closed_m5) >= 21 else 0.0
        )
        volume_ratio = (
            closed_m5[-1].get("volume", 0.0) / avg_volume
            if avg_volume > 0 else 1.0
        )

        long_crossed, long_retest, long_retest_time = breakout_retest(
            m5, "LONG", trigger_long, atr_m5
        )
        short_crossed, short_retest, short_retest_time = breakout_retest(
            m5, "SHORT", trigger_short, atr_m5
        )

        def score(side):
            score_value = 0.0

            if side == "LONG":
                score_value += 22 if h1_state == "UP" else 0
                score_value += 15 if m15_state == "UP" else 0
                score_value += 8 if m5_state == "UP" else 0

                if price > m15_20:
                    score_value += 5

                if 48 <= rsi(m15_close) <= 68:
                    score_value += 6

                if adx >= ADX_MIN and plus_di > minus_di:
                    score_value += 8

                if price > vw:
                    score_value += 7

                if volume_ratio >= 1.10:
                    score_value += 5

                if long_crossed and long_retest:
                    score_value += 14

                if price >= vp["vah"] or price >= vp["poc"]:
                    score_value += 5

            else:
                score_value += 22 if h1_state == "DOWN" else 0
                score_value += 15 if m15_state == "DOWN" else 0
                score_value += 8 if m5_state == "DOWN" else 0

                if price < m15_20:
                    score_value += 5

                if 32 <= rsi(m15_close) <= 52:
                    score_value += 6

                if adx >= ADX_MIN and minus_di > plus_di:
                    score_value += 8

                if price < vw:
                    score_value += 7

                if volume_ratio >= 1.10:
                    score_value += 5

                if short_crossed and short_retest:
                    score_value += 14

                if price <= vp["val"] or price <= vp["poc"]:
                    score_value += 5

            return min(100.0, score_value)

        long_score = score("LONG")
        short_score = score("SHORT")

        side = "LONG" if long_score >= short_score else "SHORT"
        score_value = max(long_score, short_score)

        trigger = trigger_long if side == "LONG" else trigger_short
        crossed = long_crossed if side == "LONG" else short_crossed
        retest = long_retest if side == "LONG" else short_retest

        trigger_distance = abs(trigger - price) / max(atr_m5, MIN_PRICE)

        hard = []

        if side == "LONG" and h1_state != "UP":
            hard.append("H1 не подтверждает LONG")
        if side == "SHORT" and h1_state != "DOWN":
            hard.append("H1 не подтверждает SHORT")

        if not crossed or not retest:
            hard.append("нет breakout → retest → confirmation")

        if side == "LONG":
            if adx < ADX_MIN or plus_di <= minus_di:
                hard.append("ADX/DI не подтверждают LONG")
            if price <= vw:
                hard.append("цена ниже VWAP")
        else:
            if adx < ADX_MIN or minus_di <= plus_di:
                hard.append("ADX/DI не подтверждают SHORT")
            if price >= vw:
                hard.append("цена выше VWAP")

        mid_low = support + (resistance - support) * 0.30
        mid_high = support + (resistance - support) * 0.70

        if mid_low < price < mid_high:
            hard.append("цена в середине диапазона")

        if trigger_distance > MAX_TRIGGER_ATR:
            hard.append(
                f"триггер слишком далеко ({trigger_distance:.2f} ATR)"
            )

        # Do not initiate inside the value area unless price has already
        # escaped it in the signal direction.
        if vp["val"] < price < vp["vah"]:
            hard.append("цена внутри Volume Profile Value Area")

        sl, _ = structural_levels(
            side, price, atr_m5, support, resistance, vp
        )

        if side == "LONG" and sl >= price:
            hard.append("структурный SL не ниже входа")
        if side == "SHORT" and sl <= price:
            hard.append("структурный SL не выше входа")

        target_data = None
        if not hard:
            target_data = build_targets(
                side,
                trigger,
                sl,
                atr_m15,
                support,
                resistance,
                vp,
            )

            if target_data is None:
                hard.append("нет цели с минимальным RR 2.0")

        status = "WAIT"
        reason = "; ".join(hard)

        if score_value >= MIN_SCORE and not hard:
            status = side
            reason = (
                "Полный quality gate пройден: "
                "H1→M15→M5, ADX/DI, VWAP, Volume Profile, "
                "breakout→retest→confirmation."
            )
        elif score_value >= WATCH_SCORE:
            status = "WAIT"
            reason = reason or "Кандидат высокого качества, но вход ещё не подтверждён."

        result = {
            **x,
            "status": status,
            "side": side if status in ("LONG", "SHORT") else side,
            "score": round(score_value, 1),
            "watch": score_value >= WATCH_SCORE,
            "price": price,
            "trigger": trigger,
            "entry": trigger,
            "h1": h1_state,
            "m15": m15_state,
            "m5": m5_state,
            "rsi": round(rsi(m15_close), 2),
            "atr": round(atr_m15, 8),
            "adx": round(adx, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "vwap": round(vw, 8),
            "poc": round(vp["poc"], 8),
            "vah": round(vp["vah"], 8),
            "val": round(vp["val"], 8),
            "hvn": [round(v, 8) for v in vp["hvn"][-5:]],
            "lvn": [round(v, 8) for v in vp["lvn"][-5:]],
            "support": support,
            "resistance": resistance,
            "volume_ratio": round(volume_ratio, 2),
            "trigger_distance_atr": round(trigger_distance, 3),
            "breakout": crossed,
            "retest": retest,
            "retest_time": (
                long_retest_time if side == "LONG" else short_retest_time
            ),
            "news_filter": "not_integrated",
            "protocol": "MOEX Protocol v1.3",
            "duration_sec": round(time.monotonic() - started, 2),
            "hard_blockers": hard,
            "reason": reason,
        }

        if target_data and not hard:
            result.update(
                {
                    "sl": target_data["sl"],
                    "tp1": target_data["tp1"],
                    "tp2": target_data["tp2"],
                    "tp3": target_data["tp3"],
                    "rr": round(target_data["rr"], 2),
                    "risk_distance": target_data["risk_distance"],
                }
            )
        else:
            result.update(
                {
                    "sl": None,
                    "tp1": None,
                    "tp2": None,
                    "tp3": None,
                    "rr": None,
                    "risk_distance": None,
                }
            )

        log.info(
            "ANALYZE DONE %s %s score=%.1f status=%s",
            kind,
            x.get("symbol"),
            score_value,
            status,
        )
        return result

    except Exception as exc:
        log.exception("ANALYZE ERROR %s", x.get("symbol"))
        return {
            **x,
            "status": "WAIT",
            "side": "WAIT",
            "score": 0,
            "watch": False,
            "reason": f"Ошибка анализа: {type(exc).__name__}: {exc}",
            "duration_sec": round(time.monotonic() - started, 2),
        }


def _deduplicate_confirmed(items):
    confirmed = [
        x for x in items
        if x.get("status") in ("LONG", "SHORT")
    ]

    # At most two quality signals, but never two contracts from the same
    # futures root.
    confirmed.sort(key=lambda x: x.get("score", 0), reverse=True)

    result = []
    roots = set()

    for item in confirmed:
        root = item.get("root") if item.get("kind") == "future" else item.get("symbol")
        if root in roots:
            continue
        roots.add(root)
        result.append(item)
        if len(result) >= 2:
            break

    return result


def run_scan():
    scan_started = time.monotonic()

    log.info("SCAN START v1.3")

    stocks = discover_stocks()
    futures = discover_futures()

    stock_candidates = stocks[:STOCK_POOL]
    future_candidates = futures[:FUTURES_POOL]

    candidates = stock_candidates + future_candidates
    results = []

    log.info(
        "STAGE2 START total=%d stocks=%d futures=%d",
        len(candidates),
        len(stock_candidates),
        len(future_candidates),
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures_map = {
            executor.submit(analyze, item): item
            for item in candidates
        }

        done_count = 0

        for future in as_completed(futures_map):
            item = futures_map[future]
            done_count += 1

            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    {
                        **item,
                        "status": "WAIT",
                        "side": "WAIT",
                        "score": 0,
                        "watch": False,
                        "reason": f"Ошибка worker: {type(exc).__name__}: {exc}",
                    }
                )

            if done_count % 4 == 0 or done_count == len(candidates):
                log.info(
                    "STAGE2 PROGRESS %d/%d",
                    done_count,
                    len(candidates),
                )

    confirmed = _deduplicate_confirmed(results)

    # If two sides are close, keep only a decisive candidate.
    if len(confirmed) > 1:
        confirmed = [
            confirmed[0]
        ] + [
            x for x in confirmed[1:]
            if confirmed[0].get("score", 0) - x.get("score", 0) >= 6
        ]

    confirmed_keys = {
        (x.get("kind"), x.get("secid"), x.get("side"))
        for x in confirmed
    }

    for item in results:
        key = (item.get("kind"), item.get("secid"), item.get("side"))
        if item.get("status") in ("LONG", "SHORT") and key not in confirmed_keys:
            item["status"] = "WAIT"
            item["reason"] = "Сигнал отфильтрован правилом max 2 quality signals."

    results.sort(
        key=lambda x: (
            x.get("status") in ("LONG", "SHORT"),
            x.get("score", 0),
        ),
        reverse=True,
    )

    asofs = [x.get("retest_time") for x in results if x.get("retest_time")]

    stocks_out = [x for x in results if x.get("kind") == "stock"]
    futures_out = [x for x in results if x.get("kind") == "future"]

    # Diagnostics only: sequential quality-gate funnel.
    # Trading rules and thresholds are unchanged.
    def funnel_count(items, predicate):
        return sum(1 for item in items if predicate(item))

    funnel = []

    # The scanner universe is already selected from the liquidity-ranked pools.
    funnel.append({"stage": "liquidity_universe", "count": len(results)})

    s60 = [x for x in results if x.get("score", 0) >= WATCH_SCORE]
    s62 = [x for x in s60 if x.get("score", 0) >= MIN_SCORE]
    funnel.append({"stage": "score_ge_watch", "count": len(s60)})
    funnel.append({"stage": "score_ge_min", "count": len(s62)})

    # From this point onward, each stage is applied to survivors of the
    # previous stage, producing a true funnel rather than overlapping counts.
    aligned = [
        x for x in s62
        if (
            x.get("side") in ("LONG", "SHORT")
            and (
                (x.get("side") == "LONG"
                 and x.get("h1") == "UP"
                 and x.get("m15") == "UP"
                 and x.get("m5") == "UP")
                or
                (x.get("side") == "SHORT"
                 and x.get("h1") == "DOWN"
                 and x.get("m15") == "DOWN"
                 and x.get("m5") == "DOWN")
            )
        )
    ]
    funnel.append({"stage": "h1_m15_m5_alignment", "count": len(aligned)})

    adx_ok = []
    for x in aligned:
        side = x.get("side")
        adx_ok.append(x) if (
            x.get("adx", 0) >= ADX_MIN
            and (
                (side == "LONG" and x.get("plus_di", 0) > x.get("minus_di", 0))
                or
                (side == "SHORT" and x.get("minus_di", 0) > x.get("plus_di", 0))
            )
        ) else None
    funnel.append({"stage": "adx_di_confirmation", "count": len(adx_ok)})

    vwap_ok = [
        x for x in adx_ok
        if (
            (x.get("side") == "LONG" and x.get("price", 0) > x.get("vwap", 0))
            or
            (x.get("side") == "SHORT" and x.get("price", 0) < x.get("vwap", 0))
        )
    ]
    funnel.append({"stage": "vwap_confirmation", "count": len(vwap_ok)})

    value_ok = [
        x for x in vwap_ok
        if not (
            x.get("val") is not None
            and x.get("vah") is not None
            and x.get("val", 0) < x.get("price", 0) < x.get("vah", 0)
        )
    ]
    funnel.append({"stage": "outside_value_area", "count": len(value_ok)})

    # Reconstruct the midrange test from the reported S/R values.
    midrange_ok = []
    for x in value_ok:
        support_value = x.get("support")
        resistance_value = x.get("resistance")
        price_value = x.get("price")
        if support_value is None or resistance_value is None or price_value is None:
            continue
        low = support_value + (resistance_value - support_value) * 0.30
        high = support_value + (resistance_value - support_value) * 0.70
        if not (low < price_value < high):
            midrange_ok.append(x)
    funnel.append({"stage": "midrange_filter", "count": len(midrange_ok)})

    breakout_ok = [x for x in midrange_ok if x.get("breakout") is True]
    funnel.append({"stage": "breakout", "count": len(breakout_ok)})

    retest_ok = [x for x in breakout_ok if x.get("retest") is True]
    funnel.append({"stage": "retest_confirmation", "count": len(retest_ok)})

    trigger_ok = [
        x for x in retest_ok
        if x.get("trigger_distance_atr") is not None
        and x.get("trigger_distance_atr", 999) <= MAX_TRIGGER_ATR
    ]
    funnel.append({"stage": "trigger_distance_le_1_5_atr", "count": len(trigger_ok)})

    sl_ok = []
    for x in trigger_ok:
        price_value = x.get("price")
        sl_value = x.get("sl")
        side = x.get("side")
        if price_value is None or sl_value is None:
            continue
        if (side == "LONG" and sl_value < price_value) or (
            side == "SHORT" and sl_value > price_value
        ):
            sl_ok.append(x)
    funnel.append({"stage": "structural_sl", "count": len(sl_ok)})

    rr_ok = [
        x for x in sl_ok
        if x.get("rr") is not None and x.get("rr", 0) >= 2.0
    ]
    funnel.append({"stage": "minimum_rr_ge_2", "count": len(rr_ok)})

    # Final status is the actual result of the complete gate before the
    # portfolio-level cap of max two unique signals.
    final_gate = [
        x for x in rr_ok
        if x.get("status") in ("LONG", "SHORT")
    ]
    funnel.append({"stage": "full_quality_gate", "count": len(final_gate)})
    funnel.append({"stage": "max_2_final_signals", "count": len(confirmed)})

    blocker_counts = Counter()
    for item in results:
        for blocker in item.get("hard_blockers") or []:
            blocker_counts[blocker] += 1

    watch_candidates = [
        x for x in results
        if x.get("score", 0) >= WATCH_SCORE
    ]
    min_score_candidates = [
        x for x in results
        if x.get("score", 0) >= MIN_SCORE
    ]

    diagnostics = {
        "total_analyzed": len(results),
        "watch_score": WATCH_SCORE,
        "min_score": MIN_SCORE,
        "watch_score_candidates": len(watch_candidates),
        "min_score_candidates": len(min_score_candidates),
        "confirmed_before_cap": len(final_gate),
        "confirmed_final": len(confirmed),
        "funnel": funnel,
        "blocker_counts": dict(
            sorted(
                blocker_counts.items(),
                key=lambda pair: pair[1],
                reverse=True,
            )
        ),
        "top_waits": [
            {
                "symbol": x.get("symbol") or x.get("secid"),
                "kind": x.get("kind"),
                "side": x.get("side"),
                "score": x.get("score"),
                "trigger_distance_atr": x.get("trigger_distance_atr"),
                "breakout": x.get("breakout"),
                "retest": x.get("retest"),
                "adx": x.get("adx"),
                "plus_di": x.get("plus_di"),
                "minus_di": x.get("minus_di"),
                "hard_blockers": x.get("hard_blockers") or [],
            }
            for x in sorted(
                watch_candidates,
                key=lambda item: item.get("score", 0),
                reverse=True,
            )[:8]
        ],
    }

    duration = round(time.monotonic() - scan_started, 2)

    log.info(
        "SCAN DONE v1.3 %.1fs stocks=%d futures=%d confirmed=%d",
        duration,
        len(stocks_out),
        len(futures_out),
        len(confirmed),
    )

    return {
        "longs": sorted(
            [x for x in results if x.get("status") == "LONG"],
            key=lambda x: x.get("score", 0),
            reverse=True,
        ),
        "shorts": sorted(
            [x for x in results if x.get("status") == "SHORT"],
            key=lambda x: x.get("score", 0),
            reverse=True,
        ),
        "watch": sorted(
            [x for x in results if x.get("status") == "WAIT"],
            key=lambda x: x.get("score", 0),
            reverse=True,
        ),
        "confirmed": confirmed,
        "stocks": stocks_out,
        "futures": futures_out,
        "meta": {
            "protocol": "MOEX Protocol v1.3",
            "stocks": len(stock_candidates),
            "futures": len(future_candidates),
            "workers": MAX_WORKERS,
            "m1_days": M1_DAYS,
            "h1_days": H1_DAYS,
            "generated": datetime.now(timezone.utc).isoformat(),
            "analysis_asof": max(asofs) if asofs else None,
            "history_mode": True,
            "news_filter": "not_integrated",
            "indicators": [
                "EMA20",
                "EMA50",
                "RSI14",
                "ATR14",
                "ADX14",
                "+DI",
                "-DI",
                "VWAP",
                "Volume Profile POC",
                "Volume Profile VAH",
                "Volume Profile VAL",
                "HVN",
                "LVN",
                "Support/Resistance",
                "Relative Volume",
            ],
            "quality_gate": [
                "liquidity",
                "H1/M15/M5 alignment",
                "breakout",
                "M5 close",
                "retest",
                "confirmation",
                "VWAP",
                "ADX/DI",
                "Volume Profile",
                "midrange filter",
                "trigger distance <= 1.5 ATR",
                "structural SL",
                "minimum RR >= 2.0",
                "max 2 quality signals",
            ],
            "duration_sec": duration,
            "diagnostics": diagnostics,
        },
    }
