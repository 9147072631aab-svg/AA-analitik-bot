import os
import requests
import traceback
from scanner import run_scan

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"


def send(chat, text):
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty")
        return False
    try:
        r = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=30,
        )
        print(f"Telegram sendMessage HTTP {r.status_code}: {r.text[:1000]}")
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            print(f"Telegram API error: {data}")
            return False
        return True
    except Exception as e:
        print(f"ERROR sending Telegram message: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def fmt(r):
    out = ["<b>📊 AA ANALITIK — MOEX v2</b>", ""]
    if r.get("errors"):
        out.append("⚠️ " + "; ".join(r["errors"][:3]))
        out.append("")

    signals = r.get("signals", [])
    if signals:
        for x in signals[:3]:
            icon = "🟢" if x.get("status") == "LONG" else "🔴"
            out += [
                f"{icon} <b>{x.get('status')} — {x.get('symbol')}</b>",
                f"Цена: <b>{x.get('price', '-')}</b>",
                f"H1: {x.get('h1', '-')} | M15: {x.get('m15', '-')} | M5: {x.get('m5', '-')}",
                f"Рейтинг: <b>{x.get('rating', '-')}/10</b> | Режим: {x.get('regime', '-')}",
                f"Поддержка: {x.get('support', '-')} | Сопротивление: {x.get('resistance', '-')}",
                f"Вход: {x.get('entry', '-')} | Триггер: {x.get('trigger', '-')}",
                f"SL: {x.get('sl', '-')} | TP1: {x.get('tp1', '-')} | TP2: {x.get('tp2', '-')} | TP3: {x.get('tp3', '-')}",
                f"R/R: {x.get('rr', '-')} | Ликвидность: {x.get('liquidity', '-')}",
                "",
            ]
        return "\n".join(out)

    out += ["🟡 <b>WAIT</b>", "Качественного входа сейчас нет.", ""]
    for x in r.get("all", [])[:6]:
        if not x.get("symbol"):
            continue
        out += [
            f"<b>{x.get('symbol')}</b>",
            f"Цена: {x.get('price', '-')} | Режим: {x.get('regime', '-')}",
            f"H1: {x.get('h1', '-')} | M15: {x.get('m15', '-')} | M5: {x.get('m5', '-')}",
            f"Поддержка: {x.get('support', '-')} | Сопротивление: {x.get('resistance', '-')}",
            f"LONG: {x.get('long_trigger', '-')} | SHORT: {x.get('short_trigger', '-')}",
            f"RSI: {x.get('rsi', '-')} | Причина: {x.get('reason', '-')}",
            "",
        ]
    return "\n".join(out)


def handle_update(u):
    try:
        m = u.get("message") or {}
        c = m.get("chat") or {}
        chat = c.get("id")
        t = (m.get("text") or "").lower().strip()
        if not chat:
            return

        print(f"Received Telegram command: {t}")

        if t in ("/start", "/help"):
            send(chat, "<b>AA Analitik Bot v2</b>\n\n/scan — полный скан\n/br — Brent\n/ng — газ\n/gold — золото\n/silv — серебро\n/status — состояние")
        elif t in ("/scan", "/status"):
            send(chat, fmt(run_scan()))
        elif t in ("/br", "/ng", "/gold", "/silv"):
            key = {"/br": "BR", "/ng": "NG", "/gold": "GOLD", "/silv": "SILV"}[t]
            send(chat, fmt(run_scan([key])))
        else:
            send(chat, "Используй /help")
    except Exception as e:
        print(f"ERROR in handle_update: {type(e).__name__}: {e}")
        traceback.print_exc()
