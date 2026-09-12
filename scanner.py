import requests
from datetime import datetime,timezone
CONTRACTS={"BR":"BR","NG":"NG","GOLD":"GOLD","SILV":"SILV"}
def candles(secid):
    u=f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}.json"
    r=requests.get(u,params={"iss.meta":"off","iss.only":"candles","candles.interval":10,"candles.limit":200},timeout=20)
    r.raise_for_status(); d=r.json()["candles"]; return [dict(zip(d["columns"],x)) for x in d["data"]]
def signal(cs,symbol):
    cl=[x["close"] for x in cs if x.get("close") is not None]
    hi=[x["high"] for x in cs if x.get("high") is not None]; lo=[x["low"] for x in cs if x.get("low") is not None]
    if len(cl)<30:return None
    p=cl[-1]; fast=sum(cl[-10:])/10; slow=sum(cl[-30:])/30; H=max(hi[-20:]); L=min(lo[-20:])
    if p>H*.999 and fast>slow:
        risk=p-L
        return {"symbol":symbol,"direction":"LONG","emoji":"🟢","rating":7.0,"price":round(p,4),"trigger":f"закрепление выше {round(H,4)}","sl":round(L,4),"tp1":round(p+risk*1.5,4),"tp2":round(p+risk*2,4),"rr":"1:2.0"}
    if p<L*1.001 and fast<slow:
        risk=H-p
        return {"symbol":symbol,"direction":"SHORT","emoji":"🔴","rating":7.0,"price":round(p,4),"trigger":f"закрепление ниже {round(L,4)}","sl":round(H,4),"tp1":round(p-risk*1.5,4),"tp2":round(p-risk*2,4),"rr":"1:2.0"}
def run_scan(selected=None):
    signals=[]; checked=[]
    for s in selected or list(CONTRACTS):
        try:
            checked.append(s); q=signal(candles(CONTRACTS[s]),s)
            if q: signals.append(q)
        except Exception: checked.append(s+":ERROR")
    return {"timestamp":datetime.now(timezone.utc).isoformat(),"checked":checked,"signals":sorted(signals,key=lambda x:x["rating"],reverse=True)}
