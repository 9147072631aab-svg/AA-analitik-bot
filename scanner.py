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

    h1 = candles(secid, 60, minimum=60)
    m15 = candles(secid, 15, minimum=55)
    m5 = candles(secid, 5, minimum=100)

    if len(h1) < 60 or len(m15) < 55 or len(m5) < 100:
        return {
            "symbol": contract["name"],
            "status": "WAIT",
            "rating": 0,
            "reason": (
                f"Недостаточно данных: H1={len(h1)} (нужно 60), "
                f"M15={len(m15)} (нужно 55), M5={len(m5)} (нужно 100)."
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

    e20_15, e50_15 = ema(c15, 20), ema(c15, 50)
    e20_5, e50_5 = ema(c5, 20), ema(c5, 50)

    h1_state = "UP" if e20h > e50h else "DOWN" if e20h < e50h else "FLAT"
    m15_state = "UP" if e20_15 > e50_15 else "DOWN" if e20_15 < e50_15 else "FLAT"
    m5_state = "UP" if e20_5 > e50_5 else "DOWN" if e20_5 < e50_5 else "FLAT"

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

    # v3 logic:
    # H1 = main direction, M15 = setup/pullback context, M5 = execution.
    # M15 is deliberately allowed to be opposite H1 during a pullback.
    # Entry still requires a fresh M5 breakout/breakdown, completed candle,
    # retest and rejection. A mere level crossing is never an entry.
    def confirmed_retest(side_name, level, lookback=12):
        bars = m5[-lookback:]
        if len(bars) < 5:
            return False
        tol = max(a * 0.12, abs(level) * 0.0005)
        breakout_i = None
        for i, b in enumerate(bars[:-1]):
            if side_name == "LONG" and b["close"] > level:
                breakout_i = i
            elif side_name == "SHORT" and b["close"] < level:
                breakout_i = i
        if breakout_i is None:
            return False
        for r in bars[breakout_i + 1:]:
            if side_name == "LONG" and r["low"] <= level + tol and r["close"] > level:
                return True
            if side_name == "SHORT" and r["high"] >= level - tol and r["close"] < level:
                return True
        return False

    long_confirmed = confirmed_retest("LONG", hi5)
    short_confirmed = confirmed_retest("SHORT", lo5)

    # M15 setup: price should be on the correct side of the broad structure.
    # This permits a pullback against H1, but rejects entries in the middle of range.
    long_m15_setup = (
        h1_state == "UP"
        and price > support + a * 0.15
        and price < resistance + a * 0.35
    )
    short_m15_setup = (
        h1_state == "DOWN"
        and price < resistance - a * 0.15
        and price > support - a * 0.35
    )

    side = None
    entry = sl = None
    setup = None

    if h1_state == "UP" and long_m15_setup and long_confirmed and vol_ok and rrsi < 72:
        side = "LONG"
        entry = price
        sl = min(support, price - a * 1.15)
        setup = "H1 uptrend + M15 pullback/setup + M5 breakout/retest"

    elif h1_state == "DOWN" and short_m15_setup and short_confirmed and vol_ok and rrsi > 28:
        side = "SHORT"
        entry = price
        sl = max(resistance, price + a * 1.15)
        setup = "H1 downtrend + M15 pullback/setup + M5 breakdown/retest"

    long_trigger = hi5
    short_trigger = lo5
    base = {
        "symbol": contract["name"],
        "price": round(price, 6),
        "regime": regime,
        "h1": h1_state,
        "m15": m15_state,
        "m5": m5_state,
        "support": round(support, 6),
        "resistance": round(resistance, 6),
        "long_trigger": round(long_trigger, 6),
        "short_trigger": round(short_trigger, 6),
        "rsi": round(rrsi, 1),
        "atr": round(a, 6),
        "liquidity": "OK" if vol_ok and q["oi"] > 0 else "CAUTION",
    }

    if not side:
        reasons = []
        if h1_state == "FLAT":
            reasons.append("H1 без направленного тренда")
        elif h1_state == "UP":
            if not long_m15_setup:
                reasons.append("LONG: цена не в рабочей зоне M15")
            elif not long_confirmed:
                reasons.append(f"LONG: нужен пробой {round(long_trigger, 6)}, закрытие M5 и ретест")
            elif not vol_ok:
                reasons.append("LONG: объём не подтверждает движение")
            else:
                reasons.append("LONG: ждём полного подтверждения")
        elif h1_state == "DOWN":
            if not short_m15_setup:
                reasons.append("SHORT: цена не в рабочей зоне M15")
            elif not short_confirmed:
                reasons.append(f"SHORT: нужен пробой {round(short_trigger, 6)}, закрытие M5 и ретест")
            elif not vol_ok:
                reasons.append("SHORT: объём не подтверждает движение")
            else:
                reasons.append("SHORT: ждём полного подтверждения")

        if not vol_ok and len(reasons) < 2:
            reasons.append("объём не подтверждает движение")

        base.update({"status": "WAIT", "rating": 5.0, "reason": "; ".join(reasons[:2])})
        return base

    risk = abs(entry - sl)
    if risk <= 0:
        return {**base, "status": "NO TRADE", "rating": 0, "reason": "Некорректный стоп."}

    f = (lambda k: entry + risk * k) if side == "LONG" else (lambda k: entry - risk * k)

    rating = min(
        10,
        round(
            7
            + (0.6 if vol_ok else 0)
            + (0.5 if q["oi"] > 0 else 0)
            + (0.4 if (m15_state == h1_state) else 0),
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
            contracts = max(0, math.floor((capital * risk_pct / 100) / per_contract))

    trigger_level = hi5 if side == "LONG" else lo5

    base.update({
        "status": side,
        "rating": rating,
        "setup": setup,
        "entry": round(entry, 6),
        "trigger": (
            f"пробой + закрытие M5 {'выше' if side == 'LONG' else 'ниже'} "
            f"{round(trigger_level, 6)} + ретест + отбой"
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
