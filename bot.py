import os,html,requests,traceback
from scanner import run_scan
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN",""); API=f"https://api.telegram.org/bot{TOKEN}"

def send(chat,text):
    r=requests.post(f"{API}/sendMessage",json={"chat_id":chat,"text":text,"parse_mode":"HTML"},timeout=30); r.raise_for_status()

def f(v,d=2):
    try:return f"{float(v):.{d}f}"
    except:return "-"

def card(x):
    icon="🟢" if x.get("status")=="LONG" else "🔴" if x.get("status")=="SHORT" else "🟡"
    z=[f"{icon} <b>{x.get('status')} — {html.escape(str(x.get('symbol')))}</b>",f"Score: <b>{f(x.get('score'),1)}/100</b> | {x.get('regime','-')}",f"Цена: <b>{f(x.get('price'))}</b> | H1 {x.get('h1','-')} / M15 {x.get('m15','-')} / M5 {x.get('m5','-')}",f"RSI M15 {f(x.get('rsi'),1)} | объём {f(x.get('volume_ratio'))}x"]
    if x.get("status") in ("LONG","SHORT"): z += [f"Вход <b>{f(x.get('entry'))}</b> | Trigger {f(x.get('trigger'))}",f"SL <b>{f(x.get('sl'))}</b> | TP1 {f(x.get('tp1'))} | TP2 {f(x.get('tp2'))} | TP3 {f(x.get('tp3'))}",f"R/R <b>{f(x.get('rr'))}</b>"]
    else:z.append("WAIT: "+html.escape(str(x.get("reason","-")))[:250])
    return "\n".join(z)

def fmt(r):
    out=["<b>📊 AA ANALITIK — MOEX</b>",f"Universe: TQBR {r['meta']['stocks']} | FORTS {r['meta']['futures']}"]
    for title,arr in [("🟢 TOP LONG — АКЦИИ",[x for x in r['longs'] if x['kind']=='stock']), ("🔴 TOP SHORT — АКЦИИ",[x for x in r['shorts'] if x['kind']=='stock']), ("🟢 TOP LONG — ФЬЮЧЕРСЫ",[x for x in r['longs'] if x['kind']=='future']), ("🔴 TOP SHORT — ФЬЮЧЕРСЫ",[x for x in r['shorts'] if x['kind']=='future'])]:
        out += ["",f"<b>{title}</b>"] + ([card(x) for x in arr[:3]] or ["— нет подтверждённых входов"])
    if not r['longs'] and not r['shorts']: out += ["","<b>🟡 WAIT</b>"]+[f"{x['symbol']} — {f(x.get('score'),1)} — {html.escape(str(x.get('reason','-')))}" for x in r['watch'][:6]]
    out += ["","⚠️ Score — рейтинг, не вероятность. Новости этим сканером не проверяются. Бот не отправляет ордера."]
    return "\n\n".join(out)

def handle(u):
    m=u.get("message") or {}; chat=(m.get("chat") or {}).get("id"); t=(m.get("text") or "").strip().lower()
    if not chat:return
    if t in ("/start","/help"):send(chat,"<b>AA Analitik Bot</b>\n\n/scan — полный скан\n/stocks — акции\n/futures — фьючерсы\n/status — состояние\n\nБот НЕ отправляет ордера.");return
    if t=="/scan":send(chat,fmt(run_scan()));return
    if t=="/stocks":
        r=run_scan();send(chat,"<b>АКЦИИ TQBR</b>\n\n"+"\n\n".join(card(x) for x in r['stocks'][:5]));return
    if t=="/futures":
        r=run_scan();send(chat,"<b>ФЬЮЧЕРСЫ FORTS</b>\n\n"+"\n\n".join(card(x) for x in r['futures'][:5]));return
    if t=="/status":send(chat,"<b>AA Analitik</b>\nMOEX ISS • TQBR + FORTS • H1/M15/M5\nНовости: не подключены\nОрдера: НЕ отправляются.");return
    send(chat,"Неизвестная команда. /help")

