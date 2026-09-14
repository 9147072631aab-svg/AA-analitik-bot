import os
import html
import queue
import threading
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from scanner import run_scan
from journal import save_scan, update_virtual_outcomes, daily_stats
from market_calendar import markets_status

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MSK = ZoneInfo("Europe/Moscow")

SCAN_QUEUE = queue.Queue(maxsize=2)
SCAN_LOCK = threading.Lock()

STATE_LOCK = threading.Lock()
SCAN_RUNNING = False
LAST_SCAN_STARTED = None
LAST_SCAN_FINISHED = None
LAST_SCAN_RESULT = None
LAST_CHAT_ID = None


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")


def send(chat, text):
    if not chat:
        return

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
        r.raise_for_status()


def f(v, d=2):
    try:
        return f"{float(v):.{d}f}"
    except Exception:
        return "-"


def candidate_card(x):
    side = x.get("side", "WAIT")
    status = x.get("status", "WAIT")
    icon = "🔥" if status in ("LONG", "SHORT") else ("🟡" if x.get("watch") else "⚪")
    label = (
        f"{side} — ВХОД ГОТОВ"
        if status in ("LONG", "SHORT")
        else (f"{side} — НАБЛЮДЕНИЕ" if x.get("watch") else "WAIT")
    )
    lines = [
        f"{icon} <b>{html.escape(str(x.get('symbol')))} — {label}</b>",
        f"Score: <b>{f(x.get('score'),1)}/100</b> | Цена: <b>{f(x.get('price'))}</b>",
        f"H1 {html.escape(str(x.get('h1','-')))} / M15 {html.escape(str(x.get('m15','-')))} / M5 {html.escape(str(x.get('m5','-')))}",
    ]
    if status in ("LONG", "SHORT"):
        lines += [
            f"Entry <b>{f(x.get('entry'))}</b> | Trigger {f(x.get('trigger'))}",
            f"SL <b>{f(x.get('sl'))}</b> | TP1 {f(x.get('tp1'))} | TP2 {f(x.get('tp2'))} | TP3 {f(x.get('tp3'))}",
            f"R/R <b>{f(x.get('rr'))}</b> | RSI {f(x.get('rsi'),1)} | Volume {f(x.get('volume_ratio'))}x",
        ]
    elif x.get("watch"):
        lines.append("Триггер: <b>" + f(x.get("trigger")) + "</b>")
        lines.append("Почему ждём: " + html.escape(str(x.get("reason","-")))[:350])
    return "\n".join(lines)


def format_scan(result):
    meta = result.get("meta", {})
    all_items = (result.get("stocks", []) or []) + (result.get("futures", []) or [])
    confirmed = sorted(
        [x for x in all_items if x.get("status") in ("LONG","SHORT")],
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )[:2]

    def top(items, side):
        return sorted(
            [x for x in items if x.get("side")==side and (x.get("watch") or x.get("status")==side)],
            key=lambda x: float(x.get("score") or 0),
            reverse=True
        )[:3]

    out = [
        "<b>📊 AA ANALITIK v3.0</b>",
        f"Просмотрено: {meta.get('screened',0)} | Анализ: {meta.get('analyzed',0)} | {meta.get('duration_sec','-')} сек.",
        "",
        "<b>🏆 LONG — АКЦИИ</b>",
        *[candidate_card(x) for x in top(result.get("stocks",[]), "LONG")],
        "",
        "<b>🏆 SHORT — АКЦИИ</b>",
        *[candidate_card(x) for x in top(result.get("stocks",[]), "SHORT")],
        "",
        "<b>🏆 LONG — ФЬЮЧЕРСЫ</b>",
        *[candidate_card(x) for x in top(result.get("futures",[]), "LONG")],
        "",
        "<b>🏆 SHORT — ФЬЮЧЕРСЫ</b>",
        *[candidate_card(x) for x in top(result.get("futures",[]), "SHORT")],
        "",
        "<b>🔥 ГОТОВЫЕ ВХОДЫ — МАКС. 2</b>",
        *([candidate_card(x) for x in confirmed] or ["Нет подтверждённых входов."]),
        "",
        "🧠 Каждый скан сохранён в журнал. Telegram получает только события.",
        "🤖 Ордера бот не отправляет.",
    ]
    return "\n".join(out)


def market_text():
    s = markets_status()
    def one(label, x):
        if x["open"]:
            extra = f" ({x.get('reason','N')})"
            return f"🟢 {label}: <b>ОТКРЫТ</b>{extra}"
        return f"🔴 {label}: <b>ЗАКРЫТ</b> ({x.get('reason','H')})"

    return (
        "<b>🏦 MOEX — СТАТУС РЫНКА</b>\n"
        f"Дата: {s['date']} | {s['time_msk']}\n\n"
        f"{one('Фондовый', s['stock'])}\n"
        f"{one('Срочный', s['futures'])}\n\n"
        + ("🟢 <b>Полный скан разрешён.</b>" if s["full_scan_allowed"]
           else "⏸️ <b>Полный скан остановлен: один из рынков закрыт.</b>")
    )


def scan_status_text():
    with STATE_LOCK:
        running = SCAN_RUNNING
        started = LAST_SCAN_STARTED or "-"
        finished = LAST_SCAN_FINISHED or "-"
        result = LAST_SCAN_RESULT or "-"

    q = SCAN_QUEUE.qsize()
    return (
        "<b>🔎 AA ANALITIK — СТАТУС СКАНА</b>\n"
        f"Состояние: <b>{'ЗАПУЩЕН' if running else 'НЕ ЗАПУЩЕН'}</b>\n"
        f"Очередь: <b>{q}</b>\n"
        f"Последний запуск: {started}\n"
        f"Последнее завершение: {finished}\n"
        f"Последний результат: {html.escape(str(result))}\n"
        f"Время проверки: {now_msk()}"
    )


def run_scan_job(chat=None, notify_events=True):
    global LAST_SCAN_RESULT, LAST_SCAN_FINISHED

    market = markets_status()
    if not market["full_scan_allowed"]:
        reason = (
            f"рынок закрыт: stock={market['stock']['reason']}, "
            f"futures={market['futures']['reason']}"
        )
        print(f"SCAN SKIPPED — {reason}", flush=True)
        with STATE_LOCK:
            LAST_SCAN_RESULT = "SKIPPED — рынок закрыт"
            LAST_SCAN_FINISHED = now_msk()

        if chat:
            send(chat, "⏸️ <b>Сканирование не запускалось.</b>\n" + market_text())
        return {"meta": {"skipped": True, "reason": reason}}, []

    with SCAN_LOCK:
        result = run_scan()
        update_virtual_outcomes(result)
        events = save_scan(result)
        print(f"JOURNAL SAVED events={len(events)}", flush=True)

    with STATE_LOCK:
        LAST_SCAN_RESULT = f"OK: analyzed={result.get('meta',{}).get('analyzed',0)}"

    if chat:
        send(chat, format_scan(result))

    if notify_events and chat and events:
        for e in events[:4]:
            send(chat, "🔔 <b>Событие</b>\n" + html.escape(e["message"]))

    return result, events


def enqueue_scan(chat=None):
    try:
        SCAN_QUEUE.put_nowait(chat)
        print(f"SCAN QUEUED chat={chat}", flush=True)
        return True
    except queue.Full:
        print("SCAN QUEUE FULL", flush=True)
        return False


def scan_worker():
    global SCAN_RUNNING, LAST_SCAN_STARTED, LAST_SCAN_FINISHED

    print("SCAN WORKER STARTED", flush=True)
    while True:
        chat = SCAN_QUEUE.get()
        with STATE_LOCK:
            SCAN_RUNNING = True
            LAST_SCAN_STARTED = now_msk()
            LAST_SCAN_FINISHED = None

        try:
            print(f"SCAN START chat={chat}", flush=True)

            if chat:
                send(chat, "🔎 <b>Сканирование запущено.</b>\nПроверяю торговый календарь MOEX…")

            run_scan_job(
                chat=chat,
                notify_events=bool(chat),
            )
            print("SCAN FINISHED", flush=True)

        except Exception as exc:
            print("SCAN WORKER ERROR:", repr(exc), flush=True)
            traceback.print_exc()
            with STATE_LOCK:
                LAST_SCAN_RESULT = f"ERROR: {str(exc)[:300]}"
            if chat:
                try:
                    send(chat, "❌ <b>Ошибка сканирования</b>\n" + html.escape(str(exc))[:1500])
                except Exception:
                    pass
        finally:
            with STATE_LOCK:
                SCAN_RUNNING = False
                if not LAST_SCAN_FINISHED:
                    LAST_SCAN_FINISHED = now_msk()
            SCAN_QUEUE.task_done()


threading.Thread(target=scan_worker, name="scan-worker", daemon=True).start()


def handle(update):
    global LAST_CHAT_ID

    print("TELEGRAM UPDATE RECEIVED", flush=True)
    message = update.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    command = (message.get("text") or "").strip().lower().split("@",1)[0]
    if not chat:
        return

    LAST_CHAT_ID = chat

    try:
        if command in ("/start","/help"):
            send(chat,
                 "<b>AA Analitik v3.1</b>\n\n"
                 "/scan или «скан» — запустить скан\n"
                 "/scanstatus — статус текущего скана\n"
                 "/market — состояние фондового/срочного рынка\n"
                 "/status — состояние системы\n"
                 "/report — статистика журнала\n\n"
                 "Автоскан: каждые 15 минут.\n"
                 "Праздники и выходные проверяются по календарю MOEX.\n"
                 "🤖 Ордера бот не отправляет.")
        elif command in ("/market", "/marketstatus", "рынок"):
            send(chat, market_text())
        elif command in ("/scanstatus", "/scan_status", "статус скана"):
            send(chat, scan_status_text())
        elif command == "/status":
            stats = daily_stats()
            send(chat,
                 f"<b>AA Analitik v3.1</b>\n"
                 f"Scanner v2.2: ON\n"
                 f"Protocol v1.3: ON\n"
                 f"Автоскан: ON\n"
                 f"MOEX calendar: ON\n"
                 f"Журнал наблюдений: ON\n"
                 f"Наблюдений: {stats['observations']}\n"
                 f"Подтверждённых сценариев: {stats['ready']}\n"
                 f"🤖 Ордера: НЕ отправляются.")
        elif command == "/report":
            s = daily_stats()
            wr = "-" if s["win_rate"] is None else f"{s['win_rate']:.1f}%"
            ar = "-" if s["avg_r"] is None else f"{s['avg_r']:+.2f}R"
            send(chat,
                 f"<b>🧠 AA ANALITIK — ЖУРНАЛ</b>\n"
                 f"Наблюдений: {s['observations']}\n"
                 f"Подтверждённых сценариев: {s['ready']}\n"
                 f"Завершено: {s['closed']}\n"
                 f"Успешных: {s['wins']}\n"
                 f"Win Rate: {wr}\n"
                 f"Средний результат: {ar}")
        elif command in ("/scan","scan","скан"):
            if enqueue_scan(chat):
                send(chat, "🟡 <b>Скан поставлен в очередь.</b>\nПроверка рынка и запуск — в фоне.")
            else:
                send(chat, "🟠 <b>Сканирование уже выполняется.</b>\nИспользуй /scanstatus.")
        else:
            send(chat,
                 "Команды:\n"
                 "/scan — запустить скан\n"
                 "/scanstatus — статус сканирования\n"
                 "/market — рынок открыт/закрыт\n"
                 "/status — система\n"
                 "/report — журнал")
    except Exception as exc:
        print("BOT ERROR:", repr(exc), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка бота</b>\n" + html.escape(str(exc))[:1500])
        except Exception:
            pass
