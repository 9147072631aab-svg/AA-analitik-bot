import os
import html
import threading
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from scanner import run_scan

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
SCAN_LOCK = threading.Lock()
MSK = ZoneInfo("Europe/Moscow")


def send(chat, text):
    text = str(text or "")
    limit = 3800
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut < 1200:
            cut = text.rfind("\n", 0, limit)
        if cut < 1200:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    for chunk in chunks or [""]:
        r = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat, "text": chunk, "parse_mode": "HTML"},
            timeout=30,
        )
        if not r.ok:
            try:
                details = r.json()
            except Exception:
                details = r.text
            raise RuntimeError(f"Telegram API {r.status_code}: {details}")


def f(v, d=2):
    try:
        return f"{float(v):.{d}f}"
    except Exception:
        return "-"


def asof_text(v):
    if not v:
        return "-"
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK)
        else:
            dt = dt.astimezone(MSK)
        return dt.strftime("%d.%m.%Y %H:%M") + " MSK"
    except Exception:
        return str(v)


def candidate_card(x, compact=False):
    side = x.get("side", "WAIT")
    status = x.get("status", "WAIT")
    if status in ("LONG", "SHORT"):
        label, icon = f"{side} — ВХОД ГОТОВ", "🔥"
    elif side in ("LONG", "SHORT") and x.get("watch"):
        label, icon = f"{side} — НАБЛЮДЕНИЕ", "🟡"
    else:
        label, icon = "WAIT", "⚪"

    lines = [
        f"{icon} <b>{html.escape(str(x.get('symbol')))} — {label}</b>",
        f"Сила сетапа: <b>{f(x.get('score'), 1)}/100</b> | {x.get('regime', '-')}",
        f"Цена: <b>{f(x.get('price'))}</b> | H1 {x.get('h1', '-')} / M15 {x.get('m15', '-')} / M5 {x.get('m5', '-')}",
    ]
    if status in ("LONG", "SHORT"):
        lines += [
            f"Вход <b>{f(x.get('entry'))}</b> | Trigger {f(x.get('trigger'))}",
            f"SL <b>{f(x.get('sl'))}</b> | TP1 {f(x.get('tp1'))} | TP2 {f(x.get('tp2'))} | TP3 {f(x.get('tp3'))}",
            f"R/R <b>{f(x.get('rr'))}</b> | RSI {f(x.get('rsi'), 1)} | объём {f(x.get('volume_ratio'))}x",
        ]
    elif side in ("LONG", "SHORT") and x.get("watch"):
        lines += [
            f"Триггер: <b>{f(x.get('trigger'))}</b> | до триггера {f(x.get('trigger_distance_atr'), 2)} ATR",
            f"Условие: {html.escape(str(x.get('trigger_condition', 'breakout + retest M5')))[:260]}",
            f"Почему ждём: {html.escape(str(x.get('reason', '-')))[:300]}",
        ]
    elif not compact:
        lines.append(f"Причина: {html.escape(str(x.get('reason', '-')))[:300]}")
    return "\n".join(lines)


def technical_summary(items):
    technical = [x for x in items if str(x.get("reason", "")).lower().startswith(("ошибка", "недостаточно свечей"))]
    if not technical:
        return ""
    insufficient = [x for x in technical if "недостаточно свечей" in str(x.get("reason", "")).lower()]
    errors = len(technical) - len(insufficient)
    parts = [f"⚪ Технически пропущено: {len(technical)}"]
    if insufficient:
        parts.append("нет достаточной истории: " + ", ".join(str(x.get("symbol", "-")) for x in insufficient[:8]))
    if errors:
        parts.append(f"ошибки анализа: {errors}")
    return "\n".join(parts)


def top(items, side, limit=3):
    pool = [x for x in items if x.get("side") == side and not str(x.get("reason", "")).lower().startswith(("ошибка", "недостаточно свечей")) and (x.get("status") == side or x.get("watch"))]
    return sorted(pool, key=lambda x: float(x.get("score") or 0), reverse=True)[:limit]


def fmt(r):
    stocks = r.get("stocks", [])
    futures = r.get("futures", [])
    allc = stocks + futures
    sl, ss = top(stocks, "LONG"), top(stocks, "SHORT")
    fl, fs = top(futures, "LONG"), top(futures, "SHORT")
    confirmed = sorted([x for x in allc if x.get("status") in ("LONG", "SHORT")], key=lambda x: float(x.get("score") or 0), reverse=True)[:4]

    meta = r.get("meta", {})
    if meta.get("market_mode") == "HISTORICAL":
        market_line = "🔵 <b>РЫНОК ЗАКРЫТ — ИСТОРИЧЕСКИЙ РЕЖИМ</b>"
        if meta.get("analysis_asof"):
            market_line += f"\nПоследняя доступная сессия: <b>{html.escape(asof_text(meta['analysis_asof']))}</b>"
    else:
        market_line = "🟢 <b>РЫНОК ОТКРЫТ / АКТУАЛЬНЫЕ ДАННЫЕ</b>"

    out = [
        "<b>📊 AA ANALITIK — MOEX v2.0</b>",
        market_line,
        f"Universe: TQBR {meta.get('stocks', 0)} | FORTS {meta.get('futures', 0)} | Quality gate: ON",
        "",
        "<b>🏆 TOP LONG — АКЦИИ</b>",
        *([candidate_card(x, compact=True) for x in sl] or ["— нет качественных кандидатов"]),
        "",
        "<b>🏆 TOP SHORT — АКЦИИ</b>",
        *([candidate_card(x, compact=True) for x in ss] or ["— нет качественных кандидатов"]),
        "",
        "<b>🏆 TOP LONG — ФЬЮЧЕРСЫ</b>",
        *([candidate_card(x, compact=True) for x in fl] or ["— нет качественных кандидатов"]),
        "",
        "<b>🏆 TOP SHORT — ФЬЮЧЕРСЫ</b>",
        *([candidate_card(x, compact=True) for x in fs] or ["— нет качественных кандидатов"]),
        "",
        "<b>🔥 ГОТОВЫЕ ВХОДЫ — МАКС. 2</b>",
    ]
    out += [candidate_card(x) for x in confirmed] or ["Нет подтверждённых входов. Ждём breakout + retest M5; в середине диапазона не входим."]

    top_objs = sl + ss + fl + fs
    extra = [x for x in allc if x.get("status") == "WAIT" and x.get("watch") and x not in top_objs]
    extra = sorted(extra, key=lambda x: float(x.get("score") or 0), reverse=True)[:4]
    out += ["", "<b>🟡 НАБЛЮДЕНИЕ — ТОЛЬКО ЛУЧШИЕ</b>"]
    out += [candidate_card(x) for x in extra] or ["Дополнительных сильных кандидатов нет."]

    tech = technical_summary(allc)
    if tech:
        out += ["", tech]

    out += [
        "",
        "<b>ℹ️ ВАЖНО</b>",
        "TOP — рейтинг силы сетапа, а не сигнал на вход.",
        "Вход — только после breakout + retest M5, подтверждения H1 и остальных фильтров.",
        "🔵 В историческом режиме цена берётся из последней доступной свечи; это НЕ текущая котировка.",
        "⚠️ Сила сетапа — рейтинг, не вероятность. Подтверждённых входов максимум 2; дубли одного базового актива отсекаются. Новости пока не подключены. Бот не отправляет ордера.",
    ]
    return "\n\n".join(out)


def scan_in_background(chat):
    if not SCAN_LOCK.acquire(blocking=False):
        send(chat, "🟡 <b>Сканирование уже выполняется.</b> Дождись текущего результата.")
        return
    try:
        send(chat, "⏳ <b>Сканирование запущено.</b>\nTQBR + FORTS • H1/M15/M5\nИспользую быстрый режим: один общий поток M1 на инструмент.")
        send(chat, fmt(run_scan()))
    except Exception as e:
        print("SCAN ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка сканирования</b>\n" + html.escape(str(e))[:1200])
        except Exception:
            pass
    finally:
        SCAN_LOCK.release()


def handle(u):
    m = u.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    t = (m.get("text") or "").strip().lower()
    if not chat:
        return
    try:
        if t in ("/start", "/help"):
            send(chat, "<b>AA Analitik Bot</b>\n\n/scan — полный быстрый скан\n/stocks — акции\n/futures — фьючерсы\n/status — состояние\n\nTOP ≠ сигнал. Бот НЕ отправляет ордера.")
        elif t == "/status":
            send(chat, "<b>AA Analitik</b>\nMOEX ISS • TQBR + FORTS • H1/M15/M5\nFast scanner: ON\nНовости: не подключены\nОрдера: НЕ отправляются.\nWebhook: работает.")
        elif t == "/scan":
            threading.Thread(target=scan_in_background, args=(chat,), daemon=True).start()
        elif t == "/stocks":
            send(chat, "⏳ <b>Сканирую акции...</b>")
            r = run_scan()
            send(chat, "<b>АКЦИИ TQBR</b>\n\n" + "\n\n".join(candidate_card(x) for x in r["stocks"][:5]))
        elif t == "/futures":
            send(chat, "⏳ <b>Сканирую фьючерсы...</b>")
            r = run_scan()
            send(chat, "<b>ФЬЮЧЕРСЫ FORTS</b>\n\n" + "\n\n".join(candidate_card(x) for x in r["futures"][:5]))
        else:
            send(chat, "Неизвестная команда. /help")
    except Exception as e:
        print("BOT ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка бота</b>\n" + html.escape(str(e))[:1200])
        except Exception:
            pass
