import os,requests
from scanner import run_scan
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
API=f"https://api.telegram.org/bot{TOKEN}"
def send(chat,text):
    if TOKEN: requests.post(f"{API}/sendMessage",json={"chat_id":chat,"text":text,"parse_mode":"HTML"},timeout=20)
def fmt(r):
    out=["<b>📊 AA ANALITIK — MOEX</b>",""]
    if not r["signals"]: return "\n".join(out+["🟡 <b>WAIT</b>","Качественных сетапов сейчас нет."])
    for s in r["signals"][:3]:
        out += [f"{s['emoji']} <b>{s['direction']} — {s['symbol']}</b>",f"Рейтинг: <b>{s['rating']}/10</b>",f"Цена: {s['price']}",f"Триггер: {s['trigger']}",f"SL: {s['sl']} | TP1: {s['tp1']} | TP2: {s['tp2']}",f"R/R: {s['rr']}",""]
    return "\n".join(out)
def handle_update(u):
    m=u.get("message") or {}; c=m.get("chat") or {}; chat=c.get("id"); t=(m.get("text") or "").lower().strip()
    if not chat:return
    if t in ("/start","/help"):
        send(chat,"<b>AA Analitik Bot</b>\n\n/scan — скан MOEX\n/br — Brent\n/ng — газ\n/gold — золото\n/silv — серебро\n/status — состояние")
    elif t in ("/scan","/status"): send(chat,fmt(run_scan()))
    elif t in ("/br","/ng","/gold","/silv"):
        send(chat,fmt(run_scan([{" /br":"BR","/ng":"NG","/gold":"GOLD","/silv":"SILV"}[t]])))
    else: send(chat,"Используй /help")
