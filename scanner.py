import os
import math
import re
import threading
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

BASE = "https://iss.moex.com/iss"
TIMEOUT = int(os.getenv("MOEX_TIMEOUT", "15"))
MAX_WORKERS = int(os.getenv("SCAN_WORKERS", "6"))
STOCK_POOL = int(os.getenv("STOCK_POOL", "8"))
FUTURES_POOL = int(os.getenv("FUTURES_POOL", "16"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "62"))
WATCH_SCORE = float(os.getenv("WATCH_SCORE", "60"))
MAX_TRIGGER_ATR = float(os.getenv("MAX_TRIGGER_ATR", "1.50"))
MAX_WATCH_TRIGGER_ATR = float(os.getenv("MAX_WATCH_TRIGGER_ATR", "1.75"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.000001"))
H1_DAYS = int(os.getenv("H1_DAYS", "10"))
M1_DAYS = int(os.getenv("M1_DAYS", "1"))
MSK = ZoneInfo("Europe/Moscow")
_thread_local = threading.local()
log = logging.getLogger("aa_scanner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def session():
    if not hasattr(_thread_local, "s"):
        _thread_local.s = requests.Session()
        _thread_local.s.headers.update({"User-Agent": "AA-Analitik/6.0"})
    return _thread_local.s


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
    last = None
    for attempt in range(3):
        try:
            r = session().get(url, params=p, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise last


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
        z.append(max(x["high"] - x["low"], abs(x["high"] - p), abs(x["low"] - p)))
        p = x["close"]
    return sum(z[-k:]) / k


def parse_dt(v):
    if not v:
        return None
    try:
        z = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(z)
        return dt if dt.tzinfo else dt.replace(tzinfo=MSK)
    except Exception:
        return None


def iso_msk(v):
    dt = parse_dt(v)
    return dt.astimezone(MSK).isoformat() if dt else None


def engine_path(kind):
    return "engines/stock/markets/shares" if kind == "stock" else "engines/futures/markets/forts"


def fetch_candles(sec, kind, interval, days):
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
            o, h, lo, c = (n(x.get(k), None) for k in ("open", "high", "low", "close"))
            if None in (o, h, lo, c) or c <= 0:
                continue
            begin = iso_msk(x.get("begin"))
            if not begin:
                continue
            out.append({
                "open": o, "high": h, "low": lo, "close": c,
                "volume": n(x.get("volume")), "begin": begin,
            })
        prev = start
        start += len(data)
        if start <= prev:
            break

    uniq = {x["begin"]: x for x in out if x.get("begin")}
    return [uniq[k] for k in sorted(uniq)]


def aggregate_minutes(raw, minutes):
    if minutes == 1:
        return raw
    buckets = {}
    step = minutes * 60
    for c in raw:
        dt = parse_dt(c.get("begin"))
        if not dt:
            continue
        ts = int(dt.timestamp())
        bucket = (ts // step) * step
        if bucket not in buckets:
            buckets[bucket] = {
                "open": c["open"], "high": c["high"], "low": c["low"],
                "close": c["close"], "volume": c.get("volume", 0.0),
                "begin": datetime.fromtimestamp(bucket, tz=timezone.utc).astimezone(MSK).isoformat(),
            }
        else:
            b = buckets[bucket]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c.get("volume", 0.0)
    return [buckets[k] for k in sorted(buckets)]


def load_timeframes(sec, kind):
    """Fast path: one H1 request plus one shared M1 stream for M15/M5.
    Only deepen history when the initial sample is genuinely insufficient.
    """
    h = fetch_candles(sec, kind, 60, H1_DAYS)
    if len(h) < 60:
        for days in (30, 90):
            h2 = fetch_candles(sec, kind, 60, days)
            if len(h2) > len(h):
                h = h2
            if len(h) >= 60:
                break

    raw = fetch_candles(sec, kind, 1, M1_DAYS)
    m = aggregate_minutes(raw, 15)
    f = aggregate_minutes(raw, 5)
    if len(m) < 40 or len(f) < 40:
        for days in (7, 20):
            raw2 = fetch_candles(sec, kind, 1, days)
            m2 = aggregate_minutes(raw2, 15)
            f2 = aggregate_minutes(raw2, 5)
            if len(m2) > len(m):
                m = m2
            if len(f2) > len(f):
                f = f2
            if len(m) >= 40 and len(f) >= 40:
                break
    return h, m, f


def snapshot(sec, kind):
    e = engine_path(kind)
    j = get(f"{BASE}/{e}/securities/{sec}.json", {"iss.meta": "off", "iss.only": "marketdata,securities"})
    a = rows(j.get("marketdata")); b = rows(j.get("securities"))
    return {**(b[0] if b else {}), **(a[0] if a else {})}


def discover_stocks():
    j = get(f"{BASE}/engines/stock/markets/shares/boards/TQBR/securities.json", {"iss.meta": "off", "iss.only": "securities,marketdata"})
    ss = {x.get("SECID"): x for x in rows(j.get("securities"))}
    mm = {x.get("SECID"): x for x in rows(j.get("marketdata"))}
    out = []
    for sec, x in ss.items():
        m = mm.get(sec, {})
        last = n(m.get("LAST"))
        if not sec or last <= 0:
            continue
        turnover, trades, vol = n(m.get("VALTODAY")), n(m.get("NUMTRADES")), n(m.get("VOLUME"))
        liq = math.log1p(turnover) + 0.4 * math.log1p(trades) + 0.1 * math.log1p(vol)
        out.append({"kind": "stock", "secid": sec, "symbol": sec, "name": x.get("SHORTNAME") or sec, "last": last, "liq": liq, "lot": max(1, n(x.get("LOTSIZE"), 1))})
    return sorted(out, key=lambda x: x["liq"], reverse=True)


def discover_futures():
    j = get(f"{BASE}/engines/futures/markets/forts/securities.json", {"iss.meta": "off", "iss.only": "securities"})
    groups = {}
    for x in rows(j.get("securities")):
        sec = x.get("SECID"); name = str(x.get("SHORTNAME") or sec or "")
        if not sec:
            continue
        m = re.match(r"^(.+)-\d{1,2}\.\d{2}$", name)
        if not m:
            continue
        root = m.group(1)
        vol, oi, tr = n(x.get("VOLUME")), n(x.get("OPENPOSITION")), n(x.get("NUMTRADES"))
        liq = math.log1p(vol) + 0.55 * math.log1p(oi) + 0.25 * math.log1p(tr)
        groups.setdefault(root, []).append({
            "kind": "future", "secid": sec, "symbol": name, "root": root,
            "liq": liq, "oi": oi, "volume": vol, "lot": max(1, n(x.get("LOTSIZE"), 1)),
            "step": n(x.get("MINSTEP"), 0.01), "step_price": n(x.get("STEPPRICE"), 0),
        })
    return sorted([max(v, key=lambda x: x["liq"]) for v in groups.values()], key=lambda x: x["liq"], reverse=True)


def analyze(x):
    started = time.monotonic()
    try:
        log.info("ANALYZE START %s %s", x.get("kind"), x.get("symbol"))
        h, m, f = load_timeframes(x["secid"], x["kind"])
        if min(len(h), len(m), len(f)) < 40:
            return {**x, "status": "WAIT", "score": 0, "side": "WAIT", "watch": False,
                    "reason": f"Недостаточно свечей (H1={len(h)}, M15={len(m)}, M5={len(f)})"}

        # Render-friendly: avoid an extra ISS snapshot request per instrument.
        # Discovery already supplies liquidity/contract metadata; price comes from the
        # latest available M5 candle, which is also correct in historical mode.
        price = f[-1]["close"]
        if price <= MIN_PRICE:
            return {**x, "status": "WAIT", "score": 0, "side": "WAIT", "watch": False,
                    "reason": "Невалидная цена последней M5 свечи"}

        H, M, F = [z["close"] for z in h], [z["close"] for z in m], [z["close"] for z in f]
        h20, h50, m20, m50, f20, f50 = ema(H,20), ema(H,50), ema(M,20), ema(M,50), ema(F,20), ema(F,50)
        hs = "UP" if h20 and h50 and h20 > h50 else "DOWN" if h20 and h50 and h20 < h50 else "FLAT"
        ms = "UP" if m20 and m50 and m20 > m50 else "DOWN" if m20 and m50 and m20 < m50 else "FLAT"
        fs = "UP" if f20 and f50 and f20 > f50 else "DOWN" if f20 and f50 and f20 < f50 else "FLAT"

        a = max(atr(m), price * 0.003, x.get("step", 0.01) * 5)
        sup = min(z["low"] for z in m[-21:-1]); res = max(z["high"] for z in m[-21:-1])
        hi = max(z["high"] for z in f[-13:-1]); lo = min(z["low"] for z in f[-13:-1])
        avg = sum(z["volume"] for z in f[-21:-1]) / 20
        vr = f[-1]["volume"] / avg if avg else 1
        R = rsi(M)
        trigger_long, trigger_short = hi, lo

        def score(side):
            if side == "LONG":
                trend = 25 if hs == "UP" else 11 if hs == "FLAT" else 0
                mtrend = 15 if ms == "UP" else 7 if ms == "FLAT" else 0
                ftrend = 10 if fs == "UP" else 5 if fs == "FLAT" else 0
                price_ema = max(0.0, min(10.0, 5.0 + ((price - m20) / max(a, MIN_PRICE)) * 3.0))
                rsi_part = max(0.0, min(8.0, 8.0 - abs(R - 55.0) * 0.18))
                trig = trigger_long; trigger_dist = abs(trig - price) / max(a, MIN_PRICE)
                trigger_part = max(0.0, 10.0 - min(trigger_dist, 2.0) * 5.0)
                range_pos = (price - sup) / max(res - sup, MIN_PRICE)
                range_part = max(0.0, min(10.0, 10.0 * (1.0 - abs(range_pos - 0.35) / 0.65)))
            else:
                trend = 25 if hs == "DOWN" else 11 if hs == "FLAT" else 0
                mtrend = 15 if ms == "DOWN" else 7 if ms == "FLAT" else 0
                ftrend = 10 if fs == "DOWN" else 5 if fs == "FLAT" else 0
                price_ema = max(0.0, min(10.0, 5.0 + ((m20 - price) / max(a, MIN_PRICE)) * 3.0))
                rsi_part = max(0.0, min(8.0, 8.0 - abs(R - 45.0) * 0.18))
                trig = trigger_short; trigger_dist = abs(price - trig) / max(a, MIN_PRICE)
                trigger_part = max(0.0, 10.0 - min(trigger_dist, 2.0) * 5.0)
                range_pos = (price - sup) / max(res - sup, MIN_PRICE)
                range_part = max(0.0, min(10.0, 10.0 * (1.0 - abs(range_pos - 0.65) / 0.65)))
            volume_part = max(0.0, min(7.0, 3.5 + (vr - 1.0) * 7.0))
            oi_part = 5.0 if x["kind"] == "future" and x.get("oi", 0) > 0 else 0.0
            raw = trend + mtrend + ftrend + price_ema + rsi_part + volume_part + oi_part + trigger_part + range_part
            return round(max(0.0, min(100.0, raw)), 1), trigger_dist, range_pos

        sl_score, long_dist, long_range = score("LONG")
        ss_score, short_dist, short_range = score("SHORT")
        side = "LONG" if sl_score >= ss_score else "SHORT"
        sc = max(sl_score, ss_score)
        trigger = trigger_long if side == "LONG" else trigger_short
        trigger_dist = long_dist if side == "LONG" else short_dist
        range_pos = long_range if side == "LONG" else short_range

        # Breakout must precede retest; the old logic could count the same/earlier candle twice.
        recent = f[-12:]
        breakout_idx = None
        for i, z in enumerate(recent[:-2]):
            if side == "LONG" and z["close"] > trigger:
                breakout_idx = i
            elif side == "SHORT" and z["close"] < trigger:
                breakout_idx = i
        crossed = breakout_idx is not None
        retest = False
        if breakout_idx is not None:
            for z in recent[breakout_idx + 1:]:
                if side == "LONG" and z["low"] <= trigger + a*0.15 and z["close"] >= trigger:
                    retest = True
                    break
                if side == "SHORT" and z["high"] >= trigger - a*0.15 and z["close"] <= trigger:
                    retest = True
                    break

        hard = []
        if trigger_dist > MAX_TRIGGER_ATR:
            hard.append(f"триггер далеко ({trigger_dist:.1f} ATR)")
        if not (crossed and retest):
            hard.append("нет breakout+retest M5")
        if side == "LONG" and hs != "UP":
            hard.append("H1 не подтверждает LONG")
        if side == "SHORT" and hs != "DOWN":
            hard.append("H1 не подтверждает SHORT")
        mid_low, mid_high = sup + (res-sup)*0.30, sup + (res-sup)*0.70
        in_mid = res > sup and mid_low < price < mid_high
        if in_mid and not crossed:
            hard.append("середина диапазона")
        if side == "LONG" and fs == "DOWN":
            hard.append("M5 против LONG")
        if side == "SHORT" and fs == "UP":
            hard.append("M5 против SHORT")

        penalty = 0.0
        if trigger_dist > 0.75: penalty += min(10.0, (trigger_dist-0.75)*10.0)
        if trigger_dist > 1.25: penalty += 8.0
        if in_mid and not crossed: penalty += 8.0
        if (side == "LONG" and fs == "DOWN") or (side == "SHORT" and fs == "UP"): penalty += 8.0
        if (side == "LONG" and hs != "UP") or (side == "SHORT" and hs != "DOWN"): penalty += 12.0
        sc = round(max(0.0, sc - penalty), 1)
        if trigger_dist > MAX_WATCH_TRIGGER_ATR:
            hard.append("движение ушло далеко от точки входа")

        setup_phase = "РЕТЕСТ ПОДТВЕРЖДЁН" if crossed and retest else "ПРОБОЙ ЕСТЬ — ЖДЁМ РЕТЕСТ" if crossed else "ПРОБОЙ НЕ БЫЛ"
        entry = trigger
        if side == "LONG":
            slv = min(sup, entry - a*0.45); risk = max(entry-slv, MIN_PRICE)
            t1, t2, t3 = entry+risk*1.5, entry+risk*2.2, entry+risk*3
        else:
            slv = max(res, entry + a*0.45); risk = max(slv-entry, MIN_PRICE)
            t1, t2, t3 = entry-risk*1.5, entry-risk*2.2, entry-risk*3

        score_gap = abs(sl_score - ss_score)
        # Confirmation is strict: score alone is never an entry signal.
        confirm_ok = (
            sc >= MIN_SCORE
            and not hard
            and crossed
            and retest
            and score_gap >= 6.0
            and trigger_dist <= MAX_TRIGGER_ATR
        )
        status = side if confirm_ok else "WAIT"
        return {
            **x, "price": price, "status": status, "score": sc, "long_score": sl_score, "short_score": ss_score,
            "side": side, "regime": "TREND UP" if hs == "UP" else "TREND DOWN" if hs == "DOWN" else "RANGE",
            "h1": hs, "m15": ms, "m5": fs,
            "data_asof": f[-1].get("begin") or m[-1].get("begin") or h[-1].get("begin"),
            "rsi": round(R,1), "support": sup, "resistance": res, "trigger": trigger, "entry": entry,
            "sl": slv, "tp1": t1, "tp2": t2, "tp3": t3, "rr": 3,
            "volume_ratio": round(vr,2), "liquidity": "OK" if x.get("liq",0) > 10 else "CAUTION",
            "watch": sc >= WATCH_SCORE and bool(hard) and trigger_dist <= MAX_WATCH_TRIGGER_ATR,
            "score_gap": round(score_gap, 1),
            "root_key": x.get("root") or x.get("symbol", ""),
            "setup_phase": setup_phase, "trigger_distance_atr": round(trigger_dist,2),
            "trigger_condition": f"{side}: пробой и ретест {trigger:.6g}",
            "reason": "; ".join(hard) if hard else "подтверждённый сетап",
        }
    except Exception as e:
        log.exception("ANALYZE ERROR %s", x.get("symbol"))
        return {**x, "status":"WAIT", "score":0, "side":"WAIT", "watch":False, "reason":"Ошибка: "+str(e)}


def run_scan():
    scan_started = time.monotonic()
    log.info("SCAN START")
    stocks = discover_stocks()
    futures = discover_futures()
    candidates = stocks[:STOCK_POOL] + futures[:FUTURES_POOL]
    log.info("SCAN UNIVERSE TQBR=%d FORTS=%d | ANALYZE=%d", len(stocks), len(futures), len(candidates))

    result = []
    # Small bounded batches keep Render Free responsive and avoid CPU/network bursts.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for i, item in enumerate(ex.map(analyze, candidates), 1):
            result.append(item)
            if i % 4 == 0 or i == len(candidates):
                log.info("SCAN PROGRESS %d/%d elapsed=%.1fs", i, len(candidates), time.monotonic()-scan_started)

    asofs = [parse_dt(x.get("data_asof")) for x in result if x.get("data_asof")]
    asofs = [x for x in asofs if x]
    now_msk = datetime.now(timezone.utc).astimezone(MSK)
    weekday, minutes = now_msk.weekday(), now_msk.hour*60 + now_msk.minute
    market_open = weekday < 5 and 10*60 <= minutes <= 23*60+50
    market_mode = "LIVE_OR_LATEST" if market_open else "HISTORICAL"

    longs = sorted([x for x in result if x.get("status")=="LONG"], key=lambda x:x.get("score",0), reverse=True)
    shorts = sorted([x for x in result if x.get("status")=="SHORT"], key=lambda x:x.get("score",0), reverse=True)
    watch = sorted([x for x in result if x.get("status")=="WAIT" and x.get("watch")], key=lambda x:x.get("score",0), reverse=True)
    errors = [{"symbol":x.get("symbol","-"),"reason":x.get("reason","-")} for x in result if str(x.get("reason","")).lower().startswith("ошибка")]
    skipped = [{"symbol":x.get("symbol","-"),"reason":x.get("reason","-")} for x in result if "недостаточно свечей" in str(x.get("reason","")).lower()]

    log.info("SCAN DONE %.1fs results=%d errors=%d", time.monotonic()-scan_started, len(result), len(errors))
    return {
        "longs": longs, "shorts": shorts, "watch": watch,
        "stocks": [x for x in result if x.get("kind")=="stock"],
        "futures": [x for x in result if x.get("kind")=="future"],
        "meta": {
            "stocks":len(stocks), "futures":len(futures), "stock_analyzed":STOCK_POOL,
            "futures_analyzed":FUTURES_POOL, "analyzed":len(result),
            "asof": max(asofs).isoformat() if asofs else None,
            "mode":market_mode, "errors":errors, "skipped":skipped,
            "duration_sec": round(time.monotonic()-scan_started, 1),
        }
    }
