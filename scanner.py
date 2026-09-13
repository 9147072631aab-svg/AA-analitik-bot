import os, math, re, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

BASE="https://iss.moex.com/iss"
TIMEOUT=int(os.getenv("MOEX_TIMEOUT","15"))
STAGE2=int(os.getenv("STAGE2","8"))
MIN_SCORE=float(os.getenv("MIN_SCORE","62"))
s=requests.Session(); s.headers.update({"User-Agent":"AA-Analitik/4.0"})

def rows(x):
    if not x: return []
    c=x.get("columns",[]); return [dict(zip(c,r)) for r in x.get("data",[])]

def n(x,d=0):
    try: return float(x) if x not in (None,"") else d
    except: return d

def get(url,p):
    r=s.get(url,params=p,timeout=TIMEOUT); r.raise_for_status(); return r.json()

def ema(a,k):
    if len(a)<k:return None
    e=sum(a[:k])/k; q=2/(k+1)
    for v in a[k:]: e=v*q+e*(1-q)
    return e

def rsi(a,k=14):
    if len(a)<k+1:return 50
    d=[a[i]-a[i-1] for i in range(len(a)-k,len(a))]; g=sum(max(v,0) for v in d)/k; l=sum(max(-v,0) for v in d)/k
    return 100 if l==0 else 100-100/(1+g/l)

def atr(c,k=14):
    if len(c)<k+1:return 0
    p=c[0]["close"]; z=[]
    for x in c[1:]: z.append(max(x["high"]-x["low"],abs(x["high"]-p),abs(x["low"]-p))); p=x["close"]
    return sum(z[-k:])/k

def engine(kind): return "stock/markets/shares" if kind=="stock" else "futures/markets/forts"

def candles(sec,kind,interval,days=10):
    e=engine(kind); now=datetime.now(timezone.utc); start=(now-timedelta(days=days)).date().isoformat()
    j=get(f"{BASE}/engines/{e}/securities/{sec}/candles.json",{"iss.meta":"off","interval":interval,"from":start,"till":now.date().isoformat()})
    out=[]
    for x in rows(j.get("candles")): out.append({"open":n(x.get("open")),"high":n(x.get("high")),"low":n(x.get("low")),"close":n(x.get("close")),"volume":n(x.get("volume"))})
    return [x for x in out if x["close"]>0]

def snapshot(sec,kind):
    e=engine(kind); j=get(f"{BASE}/engines/{e}/securities/{sec}.json",{"iss.meta":"off","iss.only":"marketdata,securities"})
    a=rows(j.get("marketdata")); b=rows(j.get("securities")); return {**(b[0] if b else {}),**(a[0] if a else {})}

def discover_stocks():
    j=get(f"{BASE}/engines/stock/markets/shares/boards/TQBR/securities.json",{"iss.meta":"off","iss.only":"securities,marketdata"})
    ss={x.get("SECID"):x for x in rows(j.get("securities"))}; mm={x.get("SECID"):x for x in rows(j.get("marketdata"))}; out=[]
    for sec,x in ss.items():
        m=mm.get(sec,{}); last=n(m.get("LAST"));
        if not sec or last<=0:continue
        turnover=n(m.get("VALTODAY")); trades=n(m.get("NUMTRADES")); vol=n(m.get("VOLUME"))
        liq=math.log1p(turnover)+.4*math.log1p(trades)+.1*math.log1p(vol)
        out.append({"kind":"stock","secid":sec,"symbol":sec,"name":x.get("SHORTNAME") or sec,"last":last,"liq":liq,"lot":max(1,n(x.get("LOTSIZE"),1))})
    return sorted(out,key=lambda x:x["liq"],reverse=True)

def discover_futures():
    j=get(f"{BASE}/engines/futures/markets/forts/securities.json",{"iss.meta":"off","iss.only":"securities"}); groups={}
    for x in rows(j.get("securities")):
        sec=x.get("SECID"); name=str(x.get("SHORTNAME") or sec or "")
        if not sec:continue
        m=re.match(r"^(.+)-\d{1,2}\.\d{2}$",name)
        if not m:continue
        root=m.group(1); vol=n(x.get("VOLUME")); oi=n(x.get("OPENPOSITION")); tr=n(x.get("NUMTRADES"))
        liq=math.log1p(vol)+.55*math.log1p(oi)+.25*math.log1p(tr)
        groups.setdefault(root,[]).append({"kind":"future","secid":sec,"symbol":name,"root":root,"liq":liq,"oi":oi,"volume":vol,"lot":max(1,n(x.get("LOTSIZE"),1)),"step":n(x.get("MINSTEP"),.01),"step_price":n(x.get("STEPPRICE"),0)})
    return [max(v,key=lambda x:x["liq"]) for v in groups.values()]

def stage1(x):
    try:
        q=snapshot(x["secid"],x["kind"]); z=x.copy(); z["last"]=n(q.get("LAST"),z.get("last",0)); z["volume"]=n(q.get("VOLUME"),z.get("volume",0)); z["oi"]=n(q.get("OPENPOSITION"),z.get("oi",0)); z["turnover"]=n(q.get("VALTODAY"),0); z["trades"]=n(q.get("NUMTRADES"),0); z["step_price"]=n(q.get("STEPPRICE"),z.get("step_price",0)); z["step"]=n(q.get("MINSTEP"),z.get("step",.01)); z["lot"]=max(1,n(q.get("LOTSIZE"),z.get("lot",1))); return z
    except Exception as e: x["error"]=str(e); return x

def analyze(x):
    try:
        h,m,f=candles(x["secid"],x["kind"],60,12),candles(x["secid"],x["kind"],15,7),candles(x["secid"],x["kind"],5,4)
        if min(len(h),len(m),len(f))<40:return {**x,"status":"WAIT","score":0,"reason":"Недостаточно свечей"}
        q=snapshot(x["secid"],x["kind"]); price=n(q.get("LAST"),f[-1]["close"]); H=[z["close"] for z in h]; M=[z["close"] for z in m]; F=[z["close"] for z in f]
        h20,h50,m20,m50,f20,f50=ema(H,20),ema(H,50),ema(M,20),ema(M,50),ema(F,20),ema(F,50)
        hs="UP" if h20>h50 else "DOWN" if h20<h50 else "FLAT"; ms="UP" if m20>m50 else "DOWN" if m20<m50 else "FLAT"; fs="UP" if f20>f50 else "DOWN" if f20<f50 else "FLAT"
        a=max(atr(m),price*.003,x.get("step",.01)*5); sup=min(z["low"] for z in m[-21:-1]); res=max(z["high"] for z in m[-21:-1]); hi=max(z["high"] for z in f[-13:-1]); lo=min(z["low"] for z in f[-13:-1]); avg=sum(z["volume"] for z in f[-21:-1])/20; vr=f[-1]["volume"]/avg if avg else 1; R=rsi(M)
        def score(side):
            v=0
            v+=25 if (hs=="UP" if side=="LONG" else hs=="DOWN") else -15 if (hs=="DOWN" if side=="LONG" else hs=="UP") else 0
            v+=15 if (ms=="UP" if side=="LONG" else ms=="DOWN") else 0; v+=10 if (fs=="UP" if side=="LONG" else fs=="DOWN") else 0
            v+=10 if (price>m20 if side=="LONG" else price<m20) else 0; v+=7 if vr>=1.15 else 0; v+=4 if x["kind"]=="future" and x.get("oi",0)>0 else 0
            v+=5 if (R<72 if side=="LONG" else R>28) else -7 if (R>78 if side=="LONG" else R<22) else 0; return max(0,min(100,v))
        sl,ss=score("LONG"),score("SHORT"); side="LONG" if sl>=ss else "SHORT"; sc=max(sl,ss); trigger=hi if side=="LONG" else lo
        crossed=any(z["close"]>trigger for z in f[-10:-1]) if side=="LONG" else any(z["close"]<trigger for z in f[-10:-1])
        retest=any(z["low"]<=trigger+a*.15 and z["close"]>trigger for z in f[-4:]) if side=="LONG" else any(z["high"]>=trigger-a*.15 and z["close"]<trigger for z in f[-4:])
        hard=[]
        if not (crossed and retest):hard.append("нет breakout+retest M5")
        if side=="LONG" and hs!="UP":hard.append("H1 не подтверждает LONG")
        if side=="SHORT" and hs!="DOWN":hard.append("H1 не подтверждает SHORT")
        if sup+(res-sup)*.3<price<sup+(res-sup)*.7:hard.append("середина диапазона")
        entry=trigger
        if side=="LONG": slv=min(sup,entry-a*.45); risk=entry-slv; t1=entry+risk*1.5; t2=entry+risk*2.2; t3=entry+risk*3
        else: slv=max(res,entry+a*.45); risk=slv-entry; t1=entry-risk*1.5; t2=entry-risk*2.2; t3=entry-risk*3
        status=side if sc>=MIN_SCORE and not hard else "WAIT"
        return {**x,"status":status,"score":round(sc,1),"price":price,"regime":"TREND UP" if hs=="UP" else "TREND DOWN" if hs=="DOWN" else "RANGE","h1":hs,"m15":ms,"m5":fs,"rsi":round(R,1),"rsi5":round(rsi(F),1),"support":sup,"resistance":res,"trigger":trigger,"entry":entry,"sl":slv,"tp1":t1,"tp2":t2,"tp3":t3,"rr":3,"volume_ratio":round(vr,2),"liquidity":"OK" if x.get("liq",0)>10 else "CAUTION","reason":"; ".join(hard) if hard else "подтверждённый сетап"}
    except Exception as e:return {**x,"status":"WAIT","score":0,"reason":"Ошибка: "+str(e)}

def run_scan():
    stocks=[stage1(x) for x in discover_stocks()]; futures=[stage1(x) for x in discover_futures()]; candidates=stocks[:STAGE2]+futures[:STAGE2]
    with ThreadPoolExecutor(max_workers=8) as ex: result=list(ex.map(analyze,candidates))
    return {"longs":sorted([x for x in result if x["status"]=="LONG"],key=lambda x:x["score"],reverse=True),"shorts":sorted([x for x in result if x["status"]=="SHORT"],key=lambda x:x["score"],reverse=True),"watch":sorted([x for x in result if x["status"]=="WAIT"],key=lambda x:x["score"],reverse=True),"stocks":[x for x in result if x["kind"]=="stock"],"futures":[x for x in result if x["kind"]=="future"],"meta":{"stocks":len(stocks),"futures":len(futures),"stage2":STAGE2,"generated":datetime.now(timezone.utc).isoformat()}}
