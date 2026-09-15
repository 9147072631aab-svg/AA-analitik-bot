import os
import threading
from datetime import datetime, timezone

import requests

from scanner import run_scan
import journal
import market_calendar

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SCAN_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()

SCAN_RUNNING = False
SCAN_THREAD = None
LAST_CHAT_ID = None
LAST_SCAN_START = None
LAST_SCAN_FINISH = None
LAST_SCAN_RESULT = None
LAST_SCAN_ERROR = None


def _now():
    return datetime.now(timezone.utc)


def _fmt_dt(value):
    if not value:
        return "—"
    try:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(value)


def send(chat_id, text):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return False
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": str(text),
                  "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"TELEGRAM SEND ERROR: {type(exc).__name__}: {exc}", flush=True)
        return False


def market_text():
    try:
        status = market_calendar.markets_status(_now())
        if isinstance(status, dict):
            def val(item):
                if isinstance(item, dict):
                    return item.get("status") or item.get("state") or item.get("text") or str(item)
                return str(item)
            return (
                "Рынок MOEX\n"
                f"Акции: {val(status.get('stock'))}\n"
                f"Фьючерсы: {val(status.get('futures'))}"
            )
        return f"Рынок MOEX\n{status}"
    except Exception as exc:
        return f"Рынок MOEX\nНЕ УДАЛОСЬ ПРОВЕРИТЬ ({type(exc).__name__})"


def scan_status_text():
    with STATE_LOCK:
        running = SCAN_RUNNING
        start = LAST_SCAN_START
        finish = LAST_SCAN_FINISH
        result = LAST_SCAN_RESULT
        error = LAST_SCAN_ERROR

    lines = [
        "Состояние сканера",
        f"Состояние: {'ЗАПУЩЕН' if running else 'НЕ ЗАПУЩЕН'}",
        f"Worker: {'RUNNING' if running else 'OK/IDLE'}",
        "Очередь: отсутствует (v3.2)",
        f"Последний старт: {_fmt_dt(start)}",
        f"Последнее завершение: {_fmt_dt(finish)}",
    ]
    if result:
        lines.append(f"Последний результат: {result}")
    if error:
        lines.append(f"Последняя ошибка: {error}")
    return "\n".join(lines)


def _result_summary(result):
    if result is None:
        return "пустой результат"
    if isinstance(result, dict):
        return (
            f"акции={len(result.get('stocks') or [])}, "
            f"фьючерсы={len(result.get('futures') or [])}, "
            f"подтверждено={len(result.get('confirmed') or [])}"
        )
    return type(result).__name__


def _extract_scan_text(result):
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)

    for key in ("telegram_text", "message", "text", "report"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value

    confirmed = result.get("confirmed") or []
    if not confirmed:
        return "Сигналов, прошедших полный quality gate, нет."

    parts = ["Подтвержденные сигналы:"]
    for item in confirmed[:2]:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue

        symbol = item.get("symbol") or item.get("ticker") or item.get("secid") or "?"
        side = item.get("side") or item.get("signal") or item.get("direction") or "?"
        row = f"{symbol}: {side}"

        if item.get("score") is not None:
            row += f", score={item['score']}"
        if item.get("entry") is not None or item.get("trigger") is not None:
            row += f", вход={item.get('entry') or item.get('trigger')}"
        if item.get("stop") is not None or item.get("sl") is not None:
            row += f", SL={item.get('stop') or item.get('sl')}"
        if item.get("tp1") is not None:
            row += f", TP1={item['tp1']}"

        parts.append(row)

    return "\n".join(parts)


def _scan_thread_target(chat_id):
    global SCAN_RUNNING, LAST_SCAN_FINISH, LAST_SCAN_ERROR, LAST_SCAN_RESULT

    print("SCAN START", flush=True)

    try:
        if not market_calendar.scan_allowed(_now()):
            message = (
                "Сканирование не запущено.\n"
                "MOEX сейчас не подтверждена как доступная для торговли."
            )
            with STATE_LOCK:
                LAST_SCAN_RESULT = "рынок закрыт/не подтверждён"
            if chat_id:
                send(chat_id, message)
            return

        result = run_scan()

        try:
            journal.save_scan(result)
        except Exception as exc:
            print(f"JOURNAL SAVE ERROR: {type(exc).__name__}: {exc}", flush=True)

        try:
            journal.update_virtual_outcomes(result)
        except Exception as exc:
            print(f"JOURNAL OUTCOME ERROR: {type(exc).__name__}: {exc}", flush=True)

        summary = _result_summary(result)
        with STATE_LOCK:
            LAST_SCAN_RESULT = summary

        if chat_id:
            send(chat_id, _extract_scan_text(result))

        print(f"SCAN FINISHED: {summary}", flush=True)

    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        with STATE_LOCK:
            LAST_SCAN_ERROR = error_text
            LAST_SCAN_RESULT = "ошибка"
        print(f"SCAN ERROR: {error_text}", flush=True)
        if chat_id:
            send(chat_id, f"Сканирование завершилось ошибкой.\n{error_text}")

    finally:
        with STATE_LOCK:
            SCAN_RUNNING = False
            LAST_SCAN_FINISH = _now()
        print("SCAN WORKER IDLE", flush=True)


def enqueue_scan(chat_id=None):
    global SCAN_THREAD, LAST_CHAT_ID, LAST_SCAN_START, SCAN_RUNNING

    with SCAN_LOCK:
        with STATE_LOCK:
            if SCAN_RUNNING:
                return False
            SCAN_RUNNING = True
            LAST_SCAN_START = _now()
            if chat_id is not None:
                LAST_CHAT_ID = chat_id

        thread = threading.Thread(
            target=_scan_thread_target,
            args=(chat_id,),
            daemon=True,
            name="aa-scan-worker",
        )
        with STATE_LOCK:
            SCAN_THREAD = thread
        thread.start()
        return True


def _help_text():
    return (
        "Мой аналитик\n\n"
        "Команды:\n"
        "/scan — запустить сканирование MOEX\n"
        "/scanstatus — состояние сканера\n"
        "/market — состояние рынка\n"
        "/report — статистика журнала\n"
        "/status — состояние бота"
    )


def _journal_report():
    try:
        stats = journal.daily_stats()
        if isinstance(stats, dict):
            return "\n".join(["Отчёт журнала"] + [f"{k}: {v}" for k, v in stats.items()])
        return f"Отчёт журнала\n{stats}"
    except Exception as exc:
        return f"Ошибка отчёта: {type(exc).__name__}: {exc}"


def handle(update):
    global LAST_CHAT_ID

    if not isinstance(update, dict):
        return

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if chat_id is not None:
        LAST_CHAT_ID = chat_id

    text = (message.get("text") or "").strip()
    if not text:
        return

    command = text.split()[0].lower().split("@", 1)[0]

    if command in ("/start", "/help", "help", "помощь"):
        send(chat_id, _help_text())
    elif command in ("/market", "market", "рынок"):
        send(chat_id, market_text())
    elif command in ("/scanstatus", "scanstatus", "статусскана"):
        send(chat_id, scan_status_text())
    elif command in ("/status", "status"):
        with STATE_LOCK:
            running = SCAN_RUNNING
        send(chat_id, f"Мой аналитик: OK\nСканер: {'RUNNING' if running else 'IDLE'}")
    elif command in ("/report", "report", "отчет", "отчёт"):
        send(chat_id, _journal_report())
    elif command in ("/scan", "scan", "скан", "сканирование"):
        if enqueue_scan(chat_id):
            send(chat_id, "Сканирование запущено в фоне.\nРезультат придёт отдельным сообщением.")
        else:
            send(chat_id, "Сканирование уже выполняется.")
    else:
        send(chat_id, "Неизвестная команда.\n\n" + _help_text())


def bot_info():
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is not set"}
    try:
        r = requests.get(f"{TELEGRAM_API}/getMe", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
