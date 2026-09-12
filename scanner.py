import os
import re
import math
import requests
from datetime import datetime, timezone

BASE = "https://iss.moex.com/iss"
ROOTS = ("BR", "NG", "GOLD", "SILV")
SPECS = {"BR": 1, "NG": 28, "GOLD": 18, "SILV": 17}

session = requests.Session()
session.headers.update({"User-Agent": "AA-Analitik/2.0"})

def num(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default

def ema(a, n):
    if len(a) < n: return None
    k = 2 / (n + 1)
    e = sum(a[:n]) / n
    for x in a[n:]: e = x * k + e * (1 - k)
    return e

def atr(cs, n=14):
    if len(cs) < n + 1: return None
    out, prev = [], cs[0]["close"]
    for c in cs[1:]:
        out.append(max(c["high"]-c["low"], abs(c["high"]-prev), abs(c["low"]-prev)))
        prev = c["close"]
    return sum(out[-n:]) / n

def rsi(a, n=14):
    if len(a) < n + 1: return 50.0
    d = [a[i]-a[i-1] for i in range(len(a)-n, len(a))]
    g = sum(max(x,0) for x in d)/n
    l = sum(max(-x,0) for x in d)/n
    return 100.0 if l == 0 else 100 - 100/(1+g/l)

def candles(secid, interval, limit=400):
    u = f"{BASE}/engines/futures/markets/forts/securities/{secid}.json"
    r = session.get(u, params={"iss.meta":"off","iss.only":"candles",
                               "candles.interval":interval,"candles.limit":limit}, timeout=20)
    r.raise_for_status()
    b = r.json().get("candles", {})
    out = []
    for row in b.get("data", []):
        x = dict(zip(b.get("columns", []), row))
        vals = [num(x.get(k), None) for k in ("open","high","low","close")]
        if None in vals: continue
        o,h,l,c = vals
        out.append({"open":o,"high":h,"low":l,"close":c,
                    "volume":num(x.get("volume")), "begin":x.get("begin")})
    return out

def snapshot(secid):
    u = f"{BASE}/engines/futures/markets/forts/securities/{secid}.json"
    r = session.get(u, params={
        "iss.meta":"off","iss.only":"marketdata,securities",
        "marketdata.columns":"SECID,LAST,NUMTRADES,VOLUME,OPENPOSITION",
        "securities.columns":"SECID,SHORTNAME,LOTSIZE,MINSTEP"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    md, sc = j.get("marketdata",{}), j.get("securities",{})
    mr = dict(zip(md.get("columns",[]),(md.get("data") or [[]])[0])) if md.get("data") else {}
    sr = dict(zip(sc.get("columns",[]),(sc.get("data") or [[]])[0])) if sc.get("data") else {}
    return {"last":num(mr.get("LAST"),None), "volume":num(mr.get("VOLUME")),
            "oi":num(mr.get("OPENPOSITION")), "lot":num(sr.get("LOTSIZE"),1),
            "step":num(sr.get("MINSTEP"),0.01), "shortname":sr.get("SHORTNAME") or secid}

def discover():
    u = f"{BASE}/engines/futures/markets/forts/securities.json"
    r = session.get(u, params={"iss.meta":"off","iss.only":"securities",
        "securities.columns":"SECID,SHORTNAME,VOLUME,OPENPOSITION,NUMTRADES"}, timeout=20)
    r.raise_for_status()
    b = r.json().get("securities", {})
    now = datetime.now(timezone.utc)
    found = {x:[] for x in ROOTS}
    for row in b.get("data", []):
        x = dict(zip(b.get("columns",[]),row))
        name = str(x.get("SHORTNAME") or "")
        m = re.match(r"^(BR|NG|GOLD|SILV)-(\d{1,2})\.(\d{2})$", name)
        if not m: continue
        root, month, yy = m.group(1), int(m.group(2)), int(m.group(3))
        try: expiry = datetime(2000+yy, month, SPECS[root], tzinfo=timezone.utc)
        except ValueError: continue
        if expiry < now: continue
        vol, oi, trades = num(x.get("VOLUME")), num(x.get("OPENPOSITION")), num(x.get("NUMTRADES"))
        liq = math.log1p(vol) + .5*math.log1p(oi) + .25*math.log1p(trades)
        found[root].append({"secid":x.get("SECID"),"name":name,"expiry":expiry,
                            "days":(expiry-now).days,"volume":vol,"liq":liq})
    chosen = {}
    for root, arr in found.items():
        arr.sort(key=lambda z:z["expiry"])
        if arr:
            front = arr[0]
            if front["days"] <= 7 and len(arr)>1 and arr[1]["volume"] >= max(front["volume"]*.05,100):
                front = arr[1]
            chosen[root] = front
    return chosen

def analyze(root, contract):
    secid = contract["secid"]
    h1, m15, m5 = candles(secid,60), candles(secid,15), candles(secid,5)
    if min(len(h1),len(m15),len(m5)) < 80:
        return {"symbol":contract["name"],"status":"WAIT","rating":0,
                "reason":"Недостаточно данных H1/M15/M5."}
    q = snapshot(secid)
    price = q["last"] or m5[-1]["close"]
    h = [x["close"] for x in h1]
    c15 = [x["close"] for x in m15]
    c5 = [x["close"] for x in m5]
    e20h, e50h = ema(h,20), ema(h,50)
    old20h = ema(h[:-5],20) if len(h)>55 else e20h
    regime = "TREND UP" if e20h and e50h and e20h>e50h and e20h>old20h else \
             "TREND DOWN" if e20h and e50h and e20h<e50h and e20h<old20h else "RANGE"
    direction = 1 if regime=="TREND UP" else -1 if regime=="TREND DOWN" else 0
    e20_15,e50_15 = ema(c15,20),ema(c15,50)
    e20_5,e50_5 = ema(c5,20),ema(c5,50)
    a = atr(m15) or price*.005
    rrsi = rsi(c15)
    r15 = m15[-21:-1]
    support = min(x["low"] for x in r15); resistance = max(x["high"] for x in r15)
    r5 = m5[-11:-1]
    hi5 = max(x["high"] for x in r5); lo5 = min(x["low"] for x in r5)
    avgvol = sum(x["volume"] for x in m5[-21:-1])/20
    vol_ok = avgvol <= 0 or m5[-1]["volume"] >= avgvol*1.15

    side = None
    if direction > 0 and e20_15>e50_15 and e20_5>e50_5:
        if price > hi5 and vol_ok and rrsi < 72:
            side, entry, sl, setup = "LONG", price, min(support,price-a*1.15), "M5 breakout + H1/M15 trend"
        elif abs(price-e20_15)<=a*.35 and price>e20_15 and rrsi<68:
            side, entry, sl, setup = "LONG", price, min(support,price-a*1.05), "M15 retest in uptrend"
    elif direction < 0 and e20_15<e50_15 and e20_5<e50_5:
        if price < lo5 and vol_ok and rrsi > 28:
            side, entry, sl, setup = "SHORT", price, max(resistance,price+a*1.15), "M5 breakdown + H1/M15 trend"
        elif abs(price-e20_15)<=a*.35 and price<e20_15 and rrsi>32:
            side, entry, sl, setup = "SHORT", price, max(resistance,price+a*1.05), "M15 retest in downtrend"

    base = {"symbol":contract["name"],"price":round(price,6),"regime":regime,
            "liquidity":"OK" if vol_ok and q["oi"]>0 else "CAUTION"}
    if not side:
        base.update({"status":"WAIT","rating":5.0,
                     "reason":"Нет одновременного подтверждения H1/M15/M5; ждём триггер."})
        return base

    risk = abs(entry-sl)
    if risk <= 0:
        return {**base,"status":"NO TRADE","rating":0,"reason":"Некорректный стоп."}
    f = (lambda k: entry+risk*k) if side=="LONG" else (lambda k: entry-risk*k)
    rating = min(10, round(7 + (.6 if vol_ok else 0) + (.5 if q["oi"]>0 else 0),1))

    capital = num(os.getenv("CAPITAL_RUB"),0)
    risk_pct = num(os.getenv("RISK_PCT"),1.0)
    usd_rub = num(os.getenv("USD_RUB"),0)
    contracts = None
    if capital>0 and usd_rub>0 and q["step"]>0:
        tick_rub = q["step"]*q["lot"]*usd_rub
        per_contract = (risk/q["step"])*tick_rub
        if per_contract>0:
            contracts = max(0, math.floor((capital*risk_pct/100)/per_contract))

    base.update({"status":side,"rating":rating,"setup":setup,"entry":round(entry,6),
        "trigger":f"закрепление M5 {'выше' if side=='LONG' else 'ниже'} {round(hi5 if side=='LONG' else lo5,6)}",
        "sl":round(sl,6),"tp1":round(f(1.5),6),"tp2":round(f(2),6),"tp3":round(f(3),6),
        "rr":"1:3.0","risk_pct":risk_pct,"contracts":contracts})
    return base

def run_scan(selected=None):
    selected = selected or list(ROOTS)
    try: contracts = discover()
    except Exception as e:
        return {"signals":[],"all":[],"errors":[f"MOEX ISS недоступен: {type(e).__name__}"]}
    all_results, errors = [], []
    for root in selected:
        c = contracts.get(root)
        if not c:
            all_results.append({"symbol":root,"status":"WAIT","rating":0,
                                "reason":"Активный контракт не найден через MOEX ISS."})
            continue
        try: all_results.append(analyze(root,c))
        except Exception as e:
            errors.append(f"{root}: {type(e).__name__}")
            all_results.append({"symbol":c["name"],"status":"WAIT","rating":0,
                                "reason":"Ошибка данных MOEX; сигнал не формируется."})
    signals = sorted([x for x in all_results if x.get("status") in ("LONG","SHORT")],
                     key=lambda x:x.get("rating",0), reverse=True)
    return {"timestamp":datetime.now(timezone.utc).isoformat(),
            "signals":signals,"all":all_results,"errors":errors}
