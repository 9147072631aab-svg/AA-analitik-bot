import os
import requests
from scanner import run_scan

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"

def send(chat, text):
    if TOKEN:
        requests.post(f"{API}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=20)

def fmt(r):
    out = ["<b>📊 AA ANALITIK — MOEX v3</b>", ""]
    if r.get("errors"):
        out.append("⚠️ " + "; ".join(r["errors"][:3]))
        out.append("")
    signals = r.get("signals", [])
    if signals:
        for x in signals[:3]:
            out += [
                f"{'🟢' if x['status']=='LONG' else '🔴'} <b>{x['status']} — {x['symbol']}</b>",
                f"Цена: <b>{x.get('price')}</b> | Рейтинг: <b>{x.get('rating')}/10</b>",
                f"H1: {x.get('h1')} | M15: {x.get('m15')} | M5: {x.get('m5')}",
                f"Вход: {x.get('entry')} | Триггер: {x.get('trigger')}",
                f"SL: {x.get('sl')} | TP1: {x.get('tp1')} | TP2: {x.get('tp2')} | TP3: {x.get('tp3')}",
                f"R/R: {x.get('rr')} | Ликвидность: {x.get('liquidity')}",
                f"Размер: {x.get('contracts')} контрактов" if x.get('contracts') is not None else "Размер: не рассчитан",
                ""
            ]
        return "\n".join(out)

    out += ["🟡 <b>WAIT</b>", "Качественного входа сейчас нет.", ""]
    for x in r.get("all", [])[:6]:
        if not x.get("symbol"): continue
        if x.get("price") is not None:
            out += [
                f"<b>{x['symbol']}</b> — {x.get('regime','-')} | цена {x.get('price')}",
                f"ТФ: H1 {x.get('h1','-')} / M15 {x.get('m15','-')} / M5 {x.get('m5','-')}",
                f"S: {x.get('support','-')} | R: {x.get('resistance','-')}",
                f"LONG-триггер: > {x.get('long_trigger','-')} | SHORT-триггер: < {x.get('short_trigger','-')}",
                f"RSI: {x.get('rsi','-')} | {x.get('reason','WAIT')}",
                ""
            ]
        else:
            out.append(f"<b>{x['symbol']}</b>: {x.get('reason','WAIT')}")
    return "\n".join(out)

def handle_update(u):
    m = u.get("message") or {}
    c = m.get("chat") or {}
    chat = c.get("id")
    t = (m.get("text") or "").lower().strip()
    if not chat: return
    if t in ("/start", "/help"):
        send(chat, "<b>AA Analitik Bot v3</b>\n\n/scan — полный скан\n/br — Brent\n/ng — газ\n/gold — золото\n/silv — серебро\n/status — состояние")
    elif t in ("/scan", "/status"):
        send(chat, fmt(run_scan()))
    elif t in ("/br", "/ng", "/gold", "/silv"):
        key = {"/br":"BR", "/ng":"NG", "/gold":"GOLD", "/silv":"SILV"}[t]
        send(chat, fmt(run_scan([key])))
    else:
        send(chat, "Используй /help")
