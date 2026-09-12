import os
import re
import math
import requests
from datetime import datetime, timezone, timedelta

BASE = "https://iss.moex.com/iss"
ROOTS = ("BR", "NG", "GOLD", "SILV")

# Fallback dates only if MOEX does not return LASTTRADEDATE.
FALLBACK_EXPIRY_DAY = {"BR": 1, "NG": 28, "GOLD": 18, "SILV": 17}

session = requests.Session()
session.headers.update({"User-Agent": "AA-Analitik/3.0"})


def num(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def ema(a, n):
    if len(a) < n:
        return None
    k = 2 / (n + 1)
    e = sum(a[:n]) / n
    for x in a[n:]:
        e = x * k + e * (1 - k)
    return e


def atr(cs, n=14):
    if len(cs) < n + 1:
        return None
    out = []
    prev = cs[0]["close"]
    for c in cs[1:]:
        out.append(max(c["high"] - c["low"],
                       abs(c["high"] - prev),
                       abs(c["low"] - prev)))
        prev = c["close"]
    return sum(out[-n:]) / n


def rsi(a, n=14):
    if len(a) < n + 1:
        return 50.0
    d = [a[i] - a[i - 1] for i in range(len(a) - n, len(a))]
    g = sum(max(x, 0) for x in d) / n
    l = sum(max(-x, 0) for x in d) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def parse_dt(v):
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def aggregate(candles_1m, minutes):
    """Aggregate lower timeframe candles into N-minute OHLCV bars."""
    if minutes <= 1:
        return candles_1m

    buckets = {}
    step = minutes * 60

    for c in candles_1m:
        dt = parse_dt(c.get("begin"))
        if not dt:
            continue
        ts = int(dt.timestamp())
        bucket_ts = (ts // step) * step
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0.0),
                "begin": datetime.fromtimestamp(
                    bucket_ts, tz=timezone.utc
                ).isoformat(),
            }
        else:
            b = buckets[bucket_ts]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c.get("volume", 0.0)

    return [buckets[k] for k in sorted(buckets)]


def fetch_raw(secid, interval, days=2, max_rows=5000):
    """
    Correct MOEX ISS candle parameters are interval/from/till/start.
    The previous v2 used candles.interval/candles.limit, which can be ignored.
    """
    end = datetime.now(timezone.utc)
    start_dt = end - timedelta(days=days)
    out = []
    start = 0

    # ISS commonly limits one response; paginate with start.
    while len(out) < max_rows:
        u = f"{BASE}/engines/futures/markets/forts/securities/{secid}/candles.json"
        params = {
            "iss.meta": "off",
            "interval": interval,
            "from": start_dt.date().isoformat(),
            "till": (end + timedelta(days=1)).date().isoformat(),
            "start": start,
        }
        r = session.get(u, params=params, timeout=20)
        r.raise_for_status()
        b = r.json().get("candles", {})
        rows = b.get("data", [])
        cols = b.get("columns", [])
        if not rows:
            break

        for row in rows:
            x = dict(zip(cols, row))
            vals = [num(x.get(k), None) for k in ("open", "high", "low", "close")]
            if None in vals:
                continue
            o, h, l, c = vals
            out.append({
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": num(x.get("volume")),
                "begin": x.get("begin"),
            })

        if len(rows) < 500:
            break
        start += len(rows)

        # Safety cap against an unexpectedly large response.
        if start >= max_rows:
            break

    # De-duplicate by candle time.
    uniq = {}
    for c in out:
        uniq[c.get("begin")] = c
    return [uniq[k] for k in sorted(uniq) if k]


def candles(secid, interval, minimum=80):
    # First use the native interval.
    days = 7 if interval == 60 else 3
    raw = fetch_raw(secid, interval, days=days, max_rows=2500)
    if len(raw) >= minimum:
        return raw

    # Fallback: obtain 1-minute candles and aggregate locally.
    # This also protects us if a particular ISS board does not expose
    # the requested interval directly.
    need_days = 5 if interval == 60 else 2
    raw1 = fetch_raw(secid, 1, days=need_days, max_rows=8000)
    agg = aggregate(raw1, interval)
    return agg


def snapshot(secid):
    u = f"{BASE}/engines/futures/markets/forts/securities/{secid}.json"
    r = session.get(u, params={
        "iss.meta": "off",
        "iss.only": "marketdata,securities",
        "marketdata.columns": "SECID,LAST,NUMTRADES,VOLUME,OPENPOSITION",
        "securities.columns": (
            "SECID,SHORTNAME,LOTSIZE,MINSTEP,LASTTRADEDATE"
        ),
    }, timeout=20)
    r.raise_for_status()
    j = r.json()
    md, sc = j.get("marketdata", {}), j.get("securities", {})
    mr = dict(zip(md.get("columns", []),
                  (md.get("data") or [[]])[0])) if md.get("data") else {}
    sr = dict(zip(sc.get("columns", []),
                  (sc.get("data") or [[]])[0])) if sc.get("data") else {}
    return {
        "last": num(mr.get("LAST"), None),
        "volume": num(mr.get("VOLUME")),
        "oi": num(mr.get("OPENPOSITION")),
        "lot": num(sr.get("LOTSIZE"), 1),
        "step": num(sr.get("MINSTEP"), 0.01),
        "shortname": sr.get("SHORTNAME") or secid,
        "lasttrade": parse_dt(sr.get("LASTTRADEDATE")),
    }


def discover():
    u = f"{BASE}/engines/futures/markets/forts/securities.json"
    r = session.get(u, params={
        "iss.meta": "off",
        "iss.only": "securities",
        "securities.columns": (
            "SECID,SHORTNAME,VOLUME,OPENPOSITION,NUMTRADES,"
            "LASTTRADEDATE,LOTSIZE,MINSTEP"
        ),
    }, timeout=20)
    r.raise_for_status()

    b = r.json().get("securities", {})
    now = datetime.now(timezone.utc)
    found = {x: [] for x in ROOTS}

    for row in b.get("data", []):
        x = dict(zip(b.get("columns", []), row))
        name = str(x.get("SHORTNAME") or "")
        m = re.match(r"^(BR|NG|GOLD|SILV)-(\d{1,2})\.(\d{2})$", name)
        if not m:
            continue

        root, month, yy = m.group(1), int(m.group(2)), int(m.group(3))
        expiry = parse_dt(x.get("LASTTRADEDATE"))

        if expiry is None:
            try:
                expiry = datetime(
                    2000 + yy, month, FALLBACK_EXPIRY_DAY[root],
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

        if expiry < now:
            continue

        vol = num(x.get("VOLUME"))
        oi = num(x.get("OPENPOSITION"))
        trades = num(x.get("NUMTRADES"))
        liq = (
            math.log1p(max(vol, 0))
            + 0.5 * math.log1p(max(oi, 0))
            + 0.25 * math.log1p(max(trades, 0))
        )

        found[root].append({
            "secid": x.get("SECID"),
            "name": name,
            "expiry": expiry,
            "days": (expiry - now).total_seconds() / 86400,
            "volume": vol,
            "liq": liq,
        })

    chosen = {}

    for root, arr in found.items():
        if not arr:
            continue

        arr.sort(key=lambda z: z["expiry"])

        # Do not trade a contract that is about to expire.
        # If front expires within 7 days, move to the next listed contract
        # when one exists. This fixes the GOLD/SILV rollover problem seen in v2.
        front = arr[0]
        if front["days"] <= 7 and len(arr) > 1:
            front = arr[1]

        # If several non-expiring contracts exist, prefer the more liquid
        # of the first two rather than blindly selecting the far contract.
        candidates = [x for x in arr if x["days"] > 7][:2]
        if candidates:
            best = max(candidates, key=lambda z: z["liq"])
            if best["liq"] > front["liq"] * 1.25:
                front = best

        chosen[root] = front

    return chosen


def analyze(root, contract):
    secid = contract["secid"]

    h1 = candles(secid, 60, minimum=80)
    m15 = candles(secid, 15, minimum=80)
    m5 = candles(secid, 5, minimum=80)

    if min(len(h1), len(m15), len(m5)) < 80:
        return {
            "symbol": contract["name"],
            "status": "WAIT",
            "rating": 0,
            "reason": (
                f"Недостаточно данных: H1={len(h1)}, "
                f"M15={len(m15)}, M5={len(m5)}."
            ),
        }

    q = snapshot(secid)
    price = q["last"] or m5[-1]["close"]

    h = [x["close"] for x in h1]
    c15 = [x["close"] for x in m15]
    c5 = [x["close"] for x in m5]

    e20h, e50h = ema(h, 20), ema(h, 50)
    old20h = ema(h[:-5], 20) if len(h) > 55 else e20h

    if e20h and e50h and e20h > e50h and e20h > old20h:
        regime = "TREND UP"
    elif e20h and e50h and e20h < e50h and e20h < old20h:
        regime = "TREND DOWN"
    else:
        regime = "RANGE"

    direction = 1 if regime == "TREND UP" else -1 if regime == "TREND DOWN" else 0

    e20_15, e50_15 = ema(c15, 20), ema(c15, 50)
    e20_5, e50_5 = ema(c5, 20), ema(c5, 50)

    a = atr(m15) or price * 0.005
    rrsi = rsi(c15)

    r15 = m15[-21:-1]
    support = min(x["low"] for x in r15)
    resistance = max(x["high"] for x in r15)

    r5 = m5[-11:-1]
    hi5 = max(x["high"] for x in r5)
    lo5 = min(x["low"] for x in r5)

    avgvol = sum(x["volume"] for x in m5[-21:-1]) / 20
    vol_ok = avgvol <= 0 or m5[-1]["volume"] >= avgvol * 1.15

    side = None

    if direction > 0 and e20_15 > e50_15 and e20_5 > e50_5:
        if price > hi5 and vol_ok and rrsi < 72:
            side = "LONG"
            entry = price
            sl = min(support, price - a * 1.15)
            setup = "M5 breakout + H1/M15 trend"
        elif abs(price - e20_15) <= a * 0.35 and price > e20_15 and rrsi < 68:
            side = "LONG"
            entry = price
            sl = min(support, price - a * 1.05)
            setup = "M15 retest in uptrend"

    elif direction < 0 and e20_15 < e50_15 and e20_5 < e50_5:
        if price < lo5 and vol_ok and rrsi > 28:
            side = "SHORT"
            entry = price
            sl = max(resistance, price + a * 1.15)
            setup = "M5 breakdown + H1/M15 trend"
        elif abs(price - e20_15) <= a * 0.35 and price < e20_15 and rrsi > 32:
            side = "SHORT"
            entry = price
            sl = max(resistance, price + a * 1.05)
            setup = "M15 retest in downtrend"

    base = {
        "symbol": contract["name"],
        "price": round(price, 6),
        "regime": regime,
        "liquidity": "OK" if vol_ok and q["oi"] > 0 else "CAUTION",
    }

    if not side:
        base.update({
            "status": "WAIT",
            "rating": 5.0,
            "reason": (
                "Нет одновременного подтверждения H1/M15/M5; "
                "ждём триггер."
            ),
        })
        return base

    risk = abs(entry - sl)
    if risk <= 0:
        return {
            **base,
            "status": "NO TRADE",
            "rating": 0,
            "reason": "Некорректный стоп.",
        }

    f = (
        (lambda k: entry + risk * k)
        if side == "LONG"
        else (lambda k: entry - risk * k)
    )

    rating = min(
        10,
        round(
            7
            + (0.6 if vol_ok else 0)
            + (0.5 if q["oi"] > 0 else 0),
            1,
        ),
    )

    capital = num(os.getenv("CAPITAL_RUB"), 0)
    risk_pct = num(os.getenv("RISK_PCT"), 1.0)
    usd_rub = num(os.getenv("USD_RUB"), 0)

    contracts = None
    if capital > 0 and usd_rub > 0 and q["step"] > 0:
        tick_rub = q["step"] * q["lot"] * usd_rub
        per_contract = (risk / q["step"]) * tick_rub
        if per_contract > 0:
            contracts = max(
                0,
                math.floor((capital * risk_pct / 100) / per_contract)
            )

    trigger_level = hi5 if side == "LONG" else lo5

    base.update({
        "status": side,
        "rating": rating,
        "setup": setup,
        "entry": round(entry, 6),
        "trigger": (
            f"закрепление M5 "
            f"{'выше' if side == 'LONG' else 'ниже'} "
            f"{round(trigger_level, 6)}"
        ),
        "sl": round(sl, 6),
        "tp1": round(f(1.5), 6),
        "tp2": round(f(2), 6),
        "tp3": round(f(3), 6),
        "rr": "1:3.0",
        "risk_pct": risk_pct,
        "contracts": contracts,
    })

    return base


def run_scan(selected=None):
    selected = selected or list(ROOTS)

    try:
        contracts = discover()
    except Exception as e:
        return {
            "signals": [],
            "all": [],
            "errors": [f"MOEX ISS недоступен: {type(e).__name__}"],
        }

    all_results, errors = [], []

    for root in selected:
        c = contracts.get(root)

        if not c:
            all_results.append({
                "symbol": root,
                "status": "WAIT",
                "rating": 0,
                "reason": "Активный контракт не найден через MOEX ISS.",
            })
            continue

        try:
            all_results.append(analyze(root, c))
        except Exception as e:
            errors.append(f"{root}: {type(e).__name__}")
            all_results.append({
                "symbol": c["name"],
                "status": "WAIT",
                "rating": 0,
                "reason": "Ошибка данных MOEX; сигнал не формируется.",
            })

    signals = sorted(
        [
            x for x in all_results
            if x.get("status") in ("LONG", "SHORT")
        ],
        key=lambda x: x.get("rating", 0),
        reverse=True,
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "all": all_results,
        "errors": errors,
    }
