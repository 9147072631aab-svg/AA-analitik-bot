import os
import html
import queue
import threading
import traceback
from zoneinfo import ZoneInfo

import requests
from scanner import run_scan
from journal import save_scan, update_virtual_outcomes, daily_stats

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MSK = ZoneInfo("Europe/Moscow")

SCAN_QUEUE = queue.Queue(maxsize=2)
SCAN_LOCK = threading.Lock()


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


def run_scan_job(chat=None, notify_events=True):
    with SCAN_LOCK:
        result = run_scan()
        update_virtual_outcomes(result)
        events = save_scan(result)
        print(f"JOURNAL SAVED events={len(events)}", flush=True)

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
    print("SCAN WORKER STARTED", flush=True)
    while True:
        chat = SCAN_QUEUE.get()
        try:
            print(f"SCAN START chat={chat}", flush=True)

            if chat:
                send(chat, "🔎 <b>Сканирование запущено</b>\nРезультат будет отправлен только один раз.")

            run_scan_job(
                chat=chat,
                notify_events=bool(chat),
            )
            print("SCAN FINISHED", flush=True)

        except Exception as exc:
            print("SCAN WORKER ERROR:", repr(exc), flush=True)
            traceback.print_exc()
            if chat:
                try:
                    send(chat, "❌ <b>Ошибка сканирования</b>\n" + html.escape(str(exc))[:1500])
                except Exception:
                    pass
        finally:
            SCAN_QUEUE.task_done()


threading.Thread(target=scan_worker, name="scan-worker", daemon=True).start()


def handle(update):
    print("TELEGRAM UPDATE RECEIVED", flush=True)
    message = update.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    command = (message.get("text") or "").strip().lower().split("@",1)[0]
    if not chat:
        return

    try:
        if command in ("/start","/help"):
            send(chat, "<b>AA Analitik v3.0</b>\n\n/scan или «скан» — полный скан\n/status — состояние\n/report — статистика журнала\n\nАвтоскан: каждые 15 минут.\nTelegram — только важные события.")
        elif command == "/status":
            stats = daily_stats()
            send(chat, f"<b>AA Analitik v3.0</b>\nScanner v2.2: ON\nProtocol v1.3: ON\nАвтоскан: ON\nЖурнал наблюдений: ON\nНаблюдений: {stats['observations']}\nПодтверждённых сценариев: {stats['ready']}\n🤖 Ордера: НЕ отправляются.")
        elif command == "/report":
            s = daily_stats()
            wr = "-" if s["win_rate"] is None else f"{s['win_rate']:.1f}%"
            ar = "-" if s["avg_r"] is None else f"{s['avg_r']:+.2f}R"
            send(chat, f"<b>🧠 AA ANALITIK — ЖУРНАЛ</b>\nНаблюдений: {s['observations']}\nПодтверждённых сценариев: {s['ready']}\nЗавершено: {s['closed']}\nУспешных: {s['wins']}\nWin Rate: {wr}\nСредний результат: {ar}")
        elif command in ("/scan","scan","скан"):
            if enqueue_scan(chat):
                send(chat, "🟡 <b>Скан поставлен в очередь.</b>")
            else:
                send(chat, "🟠 <b>Сканирование уже выполняется.</b>")
        else:
            send(chat, "Используй /scan, «скан», /status или /report.")
    except Exception as exc:
        print("BOT ERROR:", repr(exc), flush=True)
        traceback.print_exc()
        try:
            send(chat, "❌ <b>Ошибка бота</b>\n" + html.escape(str(exc))[:1500])
        except Exception:
            pass
