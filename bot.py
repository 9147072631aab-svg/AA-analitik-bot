import os
import html
import requests
import traceback
import threading

from scanner import run_scan

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


def send(chat, text):
    r = requests.post(f"{API}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
    r.raise_for_status()


def f(v, d=2):
    try:
        return f"{float(v):.{d}f}"
    except Exception:
        return "-"


def card(x, wait=True):
    status = x.get("status", "WAIT")
    icon = "🟢" if status == "LONG" else "🔴" if status == "SHORT" else "🟡"
    z = [
        f"{icon} <b>{html.escape(str(x.get('symbol')))} — {status}</b>",
        f"Score: <b>{f(x.get('score'), 1)}/100</b> | {x.get('regime', '-')}",
        f"Цена: <b>{f(x.get('price'))}</b> | H1 {x.get('h1', '-')} / M15 {x.get('m15', '-')} / M5 {x.get('m5', '-')}",
        f"RSI M15 {f(x.get('rsi'), 1)} | объём {f(x.get('volume_ratio'))}x",
    ]
    if status in ("LONG", "SHORT"):
        z += [
            f"Вход <b>{f(x.get('entry'))}</b> | Trigger {f(x.get('trigger'))}",
            f"SL <b>{f(x.get('sl'))}</b> | TP1 {f(x.get('tp1'))} | TP2 {f(x.get('tp2'))} | TP3 {f(x.get('tp3'))}",
            f"R/R <b>{f(x.get('rr'))}</b>",
        ]
    elif wait:
        z.append("WAIT: " + html.escape(str(x.get("reason", "-")))[:350])
    return "\n".join(z)


def candidate_card(x):
    side = x.get("side", "WAIT")
    icon = "🟢" if side == "LONG" else "🔴" if side == "SHORT" else "🟡"
    return "\n".join([
        f"{icon} <b>{html.escape(str(x.get('symbol')))} — {side}</b>",
        f"Score: <b>{f(x.get('score'), 1)}/100</b> | статус: <b>{x.get('status', 'WAIT')}</b>",
        f"Цена: <b>{f(x.get('price'))}</b> | H1 {x.get('h1', '-')} / M15 {x.get('m15', '-')} / M5 {x.get('m5', '-')}",
        f"Причина: {html.escape(str(x.get('reason', '-')))[:350]}",
    ])


def top_by_side(items, side, limit=3):
    return sorted(
        [x for x in items if x.get("side") == side],
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )[:limit]


def top_any(items, limit=3):
    return sorted(items, key=lambda x: float(x.get("score") or 0), reverse=True)[:limit]


def section(title, items):
    out = [f"<b>{title}</b>"]
    out += [candidate_card(x) for x in items] or ["— кандидатов нет"]
    return "\n\n".join(out)


def fmt(r):
    stocks = r.get("stocks", [])
    futures = r.get("futures", [])
    allc = stocks + futures

    stock_long = top_by_side(stocks, "LONG")
    stock_short = top_by_side(stocks, "SHORT")
    future_long = top_by_side(futures, "LONG")
    future_short = top_by_side(futures, "SHORT")

    # Fallback for old scanner results without side: futures stay visible.
    if not future_long and not future_short and futures:
        future_long = top_any(futures, 3)

    confirmed = sorted(
        [x for x in allc if x.get("status") in ("LONG", "SHORT")],
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )[:6]

    out = [
        "<b>📊 AA ANALITIK — MOEX</b>",
        f"Universe: TQBR {r['meta']['stocks']} | FORTS {r['meta']['futures']}",
        "",
        section("🏆 TOP LONG — АКЦИИ", stock_long),
        "",
        section("🏆 TOP SHORT — АКЦИИ", stock_short),
        "",
        section("🏆 TOP LONG — ФЬЮЧЕРСЫ", future_long),
        "",
        section("🏆 TOP SHORT — ФЬЮЧЕРСЫ", future_short),
        "",
        "<b>🔥 ПОДТВЕРЖДЁННЫЕ ВХОДЫ</b>",
    ]
    out += [card(x, wait=False) for x in confirmed] or ["Пока нет подтверждённого breakout + retest. Ждём триггер; в середине диапазона не входим."]
    out += [
        "",
        "<b>ℹ️ TOP ≠ сигнал на вход</b>",
        "TOP показывает лучшие кандидаты отдельно по акциям и фьючерсам. Вход — только после выполнения условий WAIT.",
        "",
        "⚠️ Score — рейтинг, не вероятность. Новости пока не подключены. Бот не отправляет ордера.",
    ]
    return "\n\n".join(out)


def scan_in_background(chat):
    try:
        send(chat, "⏳ <b>Сканирование запущено.</b>\nПроверяю TQBR + FORTS, H1/M15/M5.\nРезультат пришлю автоматически.")
        send(chat, fmt(run_scan()))
    except Exception as e:
        print("SCAN ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка сканирования</b>\n" + html.escape(str(e))[:1200])
        except Exception:
            pass


def handle(u):
    m = u.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    t = (m.get("text") or "").strip().lower()
    if not chat:
        return
    try:
        if t in ("/start", "/help"):
            send(chat, "<b>AA Analitik Bot</b>\n\n/scan — полный скан\n/stocks — акции\n/futures — фьючерсы\n/status — состояние\n\nTOP показывает лучшие LONG/SHORT кандидаты, даже если сейчас WAIT.\nБот НЕ отправляет ордера.")
            return
        if t == "/status":
            send(chat, "<b>AA Analitik</b>\nMOEX ISS • TQBR + FORTS • H1/M15/M5\nНовости: не подключены\nОрдера: НЕ отправляются.\nWebhook: работает.")
            return
        if t == "/scan":
            threading.Thread(target=scan_in_background, args=(chat,), daemon=True).start()
            return
        if t == "/stocks":
            send(chat, "⏳ <b>Сканирую акции...</b>")
            r = run_scan()
            send(chat, "<b>АКЦИИ TQBR</b>\n\n" + "\n\n".join(card(x) for x in r["stocks"][:5]))
            return
        if t == "/futures":
            send(chat, "⏳ <b>Сканирую фьючерсы...</b>")
            r = run_scan()
            send(chat, "<b>ФЬЮЧЕРСЫ FORTS</b>\n\n" + "\n\n".join(card(x) for x in r["futures"][:5]))
            return
        send(chat, "Неизвестная команда. /help")
    except Exception as e:
        print("BOT ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка бота</b>\n" + html.escape(str(e))[:1200])
        except Exception:
            pass
