import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

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

# Manual trade-entry workflow.
# The bot records trades only; it never sends orders to the broker.
TRADE_DRAFTS = {}
TRADE_DRAFT_LOCK = threading.Lock()

TRADE_DB_PATH = os.getenv("AA_JOURNAL_DB", "/tmp/aa_analitik_journal.sqlite3")

TRADE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts TEXT NOT NULL,
    chat_id TEXT,
    market TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    quantity REAL,
    risk_rub REAL,
    rr REAL,
    strategy TEXT,
    timeframe TEXT,
    scanner_score REAL,
    scanner_trigger REAL,
    scanner_price REAL,
    scanner_rr REAL,
    scanner_regime TEXT,
    scanner_h1 TEXT,
    scanner_m15 TEXT,
    scanner_m5 TEXT,
    scanner_rsi REAL,
    scanner_volume_ratio REAL,
    scanner_trigger_distance_atr REAL,
    scanner_reason TEXT,
    notes TEXT,
    status TEXT DEFAULT 'OPEN',
    close_price REAL,
    result_r REAL,
    closed_ts TEXT,
    close_side TEXT,
    parent_trade_id INTEGER,
    close_notes TEXT
);
"""


def _now():
    return datetime.now(timezone.utc)


def _fmt_dt(value):
    if not value:
        return "—"
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(value)


def send(chat_id, text):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return False
    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": str(text),
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"TELEGRAM SEND ERROR: {type(exc).__name__}: {exc}", flush=True)
        return False


def market_text():
    try:
        status = market_calendar.markets_status(_now())

        if isinstance(status, dict):
            def value(item):
                if isinstance(item, dict):
                    return (
                        item.get("status")
                        or item.get("state")
                        or item.get("text")
                        or str(item)
                    )
                return str(item)

            return (
                "Рынок MOEX\n"
                f"Акции: {value(status.get('stock'))}\n"
                f"Фьючерсы: {value(status.get('futures'))}"
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
        "Очередь: отсутствует (v3.2.5)",
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
    meta = result.get("meta") or {}
    diagnostics = meta.get("diagnostics") or {}

    if not confirmed:
        parts = ["Сигналов, прошедших полный quality gate, нет."]

        total = diagnostics.get("total_analyzed")
        min_score = diagnostics.get("min_score")
        watch_score = diagnostics.get("watch_score")
        min_count = diagnostics.get("min_score_candidates")
        watch_count = diagnostics.get("watch_score_candidates")

        if total is not None:
            parts.append(f"Проверено: {total}")
        if min_count is not None and min_score is not None:
            parts.append(f"Score >= {min_score:g}: {min_count}")
        if watch_count is not None and watch_score is not None:
            parts.append(f"Score >= {watch_score:g}: {watch_count}")

        blocker_counts = diagnostics.get("blocker_counts") or {}
        if blocker_counts:
            parts.append("")
            parts.append("Главные блокеры quality gate:")
            for blocker, count in list(blocker_counts.items())[:6]:
                parts.append(f"• {blocker}: {count}")

        top_waits = diagnostics.get("top_waits") or []
        if top_waits:
            parts.append("")
            parts.append("Лучшие кандидаты и причины WAIT:")
            for item in top_waits[:5]:
                symbol = item.get("symbol") or "?"
                side = item.get("side") or "WAIT"
                score = item.get("score")
                label = f"{symbol} {side}"
                if score is not None:
                    label += f" ({score})"
                parts.append(label)

                blockers = item.get("hard_blockers") or []
                for blocker in blockers[:4]:
                    parts.append(f"  • {blocker}")

        return "\n".join(parts)

    parts = ["Подтвержденные сигналы:"]

    for item in confirmed[:2]:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue

        symbol = (
            item.get("symbol")
            or item.get("ticker")
            or item.get("secid")
            or "?"
        )

        side = (
            item.get("side")
            or item.get("signal")
            or item.get("direction")
            or "?"
        )

        row = f"{symbol}: {side}"

        if item.get("score") is not None:
            row += f", score={item['score']}"

        entry = item.get("entry") or item.get("trigger")
        stop = item.get("stop") or item.get("sl")
        tp1 = item.get("tp1") or item.get("take_profit_1")

        if entry is not None:
            row += f", вход={entry}"
        if stop is not None:
            row += f", SL={stop}"
        if tp1 is not None:
            row += f", TP1={tp1}"

        parts.append(row)

    return "\n".join(parts)


def _scan_thread_target(chat_id=None):
    global SCAN_RUNNING
    global LAST_SCAN_FINISH
    global LAST_SCAN_ERROR
    global LAST_SCAN_RESULT

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
            print(
                f"JOURNAL SAVE ERROR: {type(exc).__name__}: {exc}",
                flush=True,
            )

        try:
            journal.update_virtual_outcomes(result)
        except Exception as exc:
            print(
                f"JOURNAL OUTCOME ERROR: {type(exc).__name__}: {exc}",
                flush=True,
            )

        summary = _result_summary(result)

        with STATE_LOCK:
            LAST_SCAN_RESULT = summary
            SCAN_RUNNING = False
            LAST_SCAN_FINISH = _now()

        print(f"SCAN FINISHED: {summary}", flush=True)

        if chat_id:
            send(chat_id, _extract_scan_text(result))

    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"

        with STATE_LOCK:
            LAST_SCAN_ERROR = error_text
            LAST_SCAN_RESULT = "ошибка"
            SCAN_RUNNING = False
            LAST_SCAN_FINISH = _now()

        print(f"SCAN ERROR: {error_text}", flush=True)

        if chat_id:
            send(
                chat_id,
                "Сканирование завершилось ошибкой.\n"
                f"{error_text}",
            )

    finally:
        with STATE_LOCK:
            if SCAN_RUNNING:
                SCAN_RUNNING = False
                LAST_SCAN_FINISH = _now()

        print("SCAN WORKER IDLE", flush=True)


def enqueue_scan(chat_id=None):
    global SCAN_THREAD
    global LAST_CHAT_ID
    global LAST_SCAN_START
    global SCAN_RUNNING

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


def _trade_db():
    con = sqlite3.connect(TRADE_DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(TRADE_SCHEMA)
    _ensure_trade_columns(con)
    return con


def _trade_num(value):
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return None


def _ensure_trade_columns(con):
    existing = {row[1] for row in con.execute("PRAGMA table_info(trades)")}
    wanted = {
        "close_side": "TEXT",
        "parent_trade_id": "INTEGER",
        "close_notes": "TEXT",
    }
    for name, sql_type in wanted.items():
        if name not in existing:
            con.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")
    con.commit()


def _latest_scan_observation(symbol, side):
    con = _trade_db()
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info(observations)")]
        if not columns:
            return {}

        row = con.execute(
            """SELECT * FROM observations
               WHERE symbol=? AND side=?
               ORDER BY id DESC LIMIT 1""",
            (symbol.upper(), side.upper()),
        ).fetchone()

        return dict(zip(columns, row)) if row else {}
    finally:
        con.close()


def _trade_menu(chat_id):
    return _send_markup(
        chat_id,
        "📝 Журнал сделок\n\n"
        "Открытие и закрытие позиции теперь являются одной сделкой.\n"
        "Если купил SBER — закрытие SBER через SELL будет привязано к этой же записи.\n\n"
        "Выбери действие:",
        {
            "inline_keyboard": [
                [{"text": "🟢 Открыть покупку / LONG", "callback_data": "trade:LONG"}],
                [{"text": "🔴 Открыть продажу / SHORT", "callback_data": "trade:SHORT"}],
                [{"text": "🔒 Закрыть позицию", "callback_data": "trade:close"}],
                [{"text": "📋 Последние сделки", "callback_data": "trade:list"}],
                [{"text": "🧹 Очистить тестовые сделки", "callback_data": "trade:reset"}],
            ]
        },
    )


def _send_markup(chat_id, text, reply_markup):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return False
    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": str(text),
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"TELEGRAM MARKUP ERROR: {type(exc).__name__}: {exc}", flush=True)
        return False


def _answer_callback(callback_id):
    if not callback_id or not TELEGRAM_BOT_TOKEN:
        return False
    try:
        response = requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"TELEGRAM CALLBACK ERROR: {type(exc).__name__}: {exc}", flush=True)
        return False


def _draft_set(chat_id, **values):
    with TRADE_DRAFT_LOCK:
        draft = TRADE_DRAFTS.setdefault(str(chat_id), {})
        draft.update(values)
        return dict(draft)


def _draft_get(chat_id):
    with TRADE_DRAFT_LOCK:
        return dict(TRADE_DRAFTS.get(str(chat_id), {}))


def _draft_clear(chat_id):
    with TRADE_DRAFT_LOCK:
        TRADE_DRAFTS.pop(str(chat_id), None)


def _trade_prompt(step):
    prompts = {
        1: "Шаг 1/9 — тикер/инструмент.\nНапример: SBER, GAZP, Si-9.26, BTC-9.26",
        2: "Шаг 2/9 — фактическая цена входа.",
        3: "Шаг 3/9 — Stop Loss (SL).",
        4: "Шаг 4/9 — Take Profit 1 (TP1).",
        5: "Шаг 5/9 — Take Profit 2 (TP2).\nЕсли не используется — отправь 0.",
        6: "Шаг 6/9 — Take Profit 3 (TP3).\nЕсли не используется — отправь 0.",
        7: "Шаг 7/9 — количество/объём позиции.",
        8: "Шаг 8/9 — риск в ₽.\nЕсли не вводишь — отправь 0.",
        9: "Шаг 9/9 — комментарий.\nЕсли комментария нет — напиши «нет».",
    }
    return prompts.get(step, "")


def _create_trade(chat_id, draft):
    symbol = draft["symbol"].upper()
    side = draft["side"]
    entry = draft["entry"]
    sl = draft["sl"]

    obs = _latest_scan_observation(symbol, side)

    risk_distance = abs(entry - sl)
    rr = None
    tp1 = draft.get("tp1")
    if risk_distance and tp1:
        reward = tp1 - entry if side == "LONG" else entry - tp1
        if reward > 0:
            rr = reward / risk_distance

    con = _trade_db()
    try:
        con.execute(
            """INSERT INTO trades (
                created_ts, chat_id, market, symbol, side, entry, sl,
                tp1, tp2, tp3, quantity, risk_rub, rr,
                strategy, timeframe, scanner_score, scanner_trigger,
                scanner_price, scanner_rr, scanner_regime, scanner_h1,
                scanner_m15, scanner_m5, scanner_rsi, scanner_volume_ratio,
                scanner_trigger_distance_atr, scanner_reason, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _now().isoformat(),
                str(chat_id),
                obs.get("market"),
                symbol,
                side,
                entry,
                sl,
                draft.get("tp1"),
                draft.get("tp2"),
                draft.get("tp3"),
                draft.get("quantity"),
                draft.get("risk_rub"),
                rr,
                "MOEX Protocol v1.3",
                "H1/M15/M5",
                obs.get("score"),
                obs.get("trigger"),
                obs.get("price"),
                obs.get("rr"),
                obs.get("regime"),
                obs.get("h1"),
                obs.get("m15"),
                obs.get("m5"),
                obs.get("rsi"),
                obs.get("volume_ratio"),
                obs.get("trigger_distance_atr"),
                str(obs.get("reason", ""))[:1000],
                draft.get("notes"),
            ),
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()
    finally:
        con.close()

    return trade_id, obs, rr


def _open_trades():
    con = _trade_db()
    try:
        rows = con.execute(
            """SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,quantity,risk_rub,
                      created_ts,scanner_score
               FROM trades
               WHERE status='OPEN'
               ORDER BY id DESC
               LIMIT 20"""
        ).fetchall()
    finally:
        con.close()
    return rows


def _close_menu(chat_id):
    rows = _open_trades()
    if not rows:
        send(chat_id, "📋 Открытых позиций нет.")
        return

    buttons = []
    for row in rows:
        trade_id, symbol, side, entry, sl, tp1, tp2, tp3, qty, risk_rub, created_ts, score = row
        direction = "🟢" if side == "LONG" else "🔴"
        label = f"{direction} #{trade_id} {symbol} {side} @ {entry}"
        buttons.append([{"text": label[:60], "callback_data": f"close:{trade_id}"}])

    buttons.append([{"text": "↩️ Назад", "callback_data": "trade:menu"}])
    _send_markup(
        chat_id,
        "🔒 Закрытие позиции\n\n"
        "Выбери открытую позицию. Продажа LONG будет записана как закрытие "
        "покупки, а покупка SHORT — как закрытие продажи.\n\n"
        "Новая сделка для закрытия не создаётся.",
        {"inline_keyboard": buttons},
    )


def _close_position_prompt(chat_id, trade_id):
    con = _trade_db()
    try:
        row = con.execute(
            """SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,quantity,risk_rub,
                      created_ts,scanner_score
               FROM trades WHERE id=? AND status='OPEN'""",
            (trade_id,),
        ).fetchone()
    finally:
        con.close()

    if not row:
        send(chat_id, "Эта позиция уже закрыта или не найдена.")
        return

    trade_id, symbol, side, entry, sl, tp1, tp2, tp3, qty, risk_rub, created_ts, score = row
    _draft_set(
        chat_id,
        close_mode=True,
        close_trade_id=trade_id,
        close_side=("SHORT" if side == "LONG" else "LONG"),
        step=1,
    )
    send(
        chat_id,
        f"🔒 Закрытие #{trade_id}: {symbol} {side}\n"
        f"Вход: {entry}\n"
        f"SL: {sl}\n\n"
        "Шаг 1/2 — фактическая цена закрытия.",
    )


def _close_trade(chat_id, draft):
    trade_id = int(draft["close_trade_id"])
    close_price = float(draft["close_price"])

    con = _trade_db()
    try:
        row = con.execute(
            """SELECT id,symbol,side,entry,sl,quantity,risk_rub,status
               FROM trades WHERE id=?""",
            (trade_id,),
        ).fetchone()

        if not row:
            raise ValueError("позиция не найдена")
        if row[7] != "OPEN":
            raise ValueError("позиция уже закрыта")

        _, symbol, side, entry, sl, quantity, risk_rub, _ = row
        risk_distance = abs(entry - sl)

        if side == "LONG":
            pnl_distance = close_price - entry
        else:
            pnl_distance = entry - close_price

        result_r = None
        if risk_distance > 0:
            result_r = pnl_distance / risk_distance

        con.execute(
            """UPDATE trades
               SET close_price=?,
                   result_r=?,
                   closed_ts=?,
                   close_side=?,
                   close_notes=?,
                   status='CLOSED'
               WHERE id=? AND status='OPEN'""",
            (
                close_price,
                result_r,
                _now().isoformat(),
                draft.get("close_side"),
                draft.get("close_notes"),
                trade_id,
            ),
        )
        con.commit()
    finally:
        con.close()

    return {
        "id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "quantity": quantity,
        "risk_rub": risk_rub,
        "close_price": close_price,
        "result_r": result_r,
    }


def _close_saved_text(position):
    r = position["result_r"]
    r_text = f"{r:+.2f} R" if r is not None else "—"
    pnl_text = "прибыль" if r is not None and r > 0 else ("убыток" if r is not None and r < 0 else "результат без R")
    return (
        "✅ Позиция закрыта\n"
        f"Сделка #{position['id']}\n"
        f"Инструмент: {position['symbol']}\n"
        f"Открытие: {position['side']} @ {position['entry']}\n"
        f"Закрытие: {'SELL' if position['side']=='LONG' else 'BUY'} @ {position['close_price']}\n"
        f"Количество: {position['quantity'] or '—'}\n"
        f"Результат: {r_text} ({pnl_text})\n\n"
        "Открытие и закрытие связаны в одной записи журнала.\n"
        "Ордера брокеру НЕ отправлялись."
    )


def _trade_list():
    con = _trade_db()
    try:
        rows = con.execute(
            """SELECT id,created_ts,symbol,side,entry,sl,tp1,tp2,tp3,
                      quantity,risk_rub,rr,status,close_price,result_r,closed_ts
               FROM trades ORDER BY id DESC LIMIT 10"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return "Журнал сделок пуст."

    lines = ["📋 Последние сделки"]
    for row in rows:
        (
            trade_id, created_ts, symbol, side, entry, sl, tp1, tp2, tp3,
            quantity, risk_rub, rr, status, close_price, result_r, closed_ts
        ) = row

        rr_text = f", RR={rr:.2f}" if rr is not None else ""
        if status == "CLOSED":
            close_text = f" → закрытие {close_price}"
            result_text = f", результат {result_r:+.2f} R" if result_r is not None else ""
            status_text = "CLOSED"
        else:
            close_text = ""
            result_text = ""
            status_text = "OPEN"

        lines.append(
            f"#{trade_id} {symbol} {side} | вход {entry} | SL {sl} | "
            f"TP1 {tp1 or '—'} | объём {quantity or '—'} | "
            f"{status_text}{close_text}{rr_text}{result_text}"
        )
    return "\n".join(lines)


def _trade_saved_text(trade_id, draft, obs, rr):
    side_text = "ПОКУПКА / LONG" if draft["side"] == "LONG" else "ПРОДАЖА / SHORT"
    lines = [
        "✅ Сделка записана в журнал",
        f"ID сделки: {trade_id}",
        f"Инструмент: {draft['symbol'].upper()}",
        f"Направление: {side_text}",
        f"Вход: {draft['entry']}",
        f"SL: {draft['sl']}",
        f"TP1: {draft.get('tp1') or '—'}",
        f"TP2: {draft.get('tp2') or '—'}",
        f"TP3: {draft.get('tp3') or '—'}",
        f"Количество: {draft.get('quantity') or '—'}",
        f"Риск ₽: {draft.get('risk_rub') or '—'}",
        f"Расчётный RR по TP1: {f'{rr:.2f}' if rr is not None else '—'}",
    ]

    if obs:
        lines += [
            "",
            "🤖 Данные последнего скана перенесены автоматически:",
            f"Score: {obs.get('score') or '—'}",
            f"Цена скана: {obs.get('price') or '—'}",
            f"Trigger: {obs.get('trigger') or '—'}",
            f"RR скана: {obs.get('rr') or '—'}",
            f"Regime: {obs.get('regime') or '—'}",
            f"H1/M15/M5: {obs.get('h1') or '—'} / {obs.get('m15') or '—'} / {obs.get('m5') or '—'}",
            f"RSI: {obs.get('rsi') or '—'}",
            f"Volume ratio: {obs.get('volume_ratio') or '—'}",
            f"Trigger distance ATR: {obs.get('trigger_distance_atr') or '—'}",
        ]

    lines += [
        "",
        "Ордера брокеру НЕ отправлялись.",
        "Запись сохранена в SQLite-журнал бота.",
    ]
    return "\n".join(lines)


def _handle_trade_callback(callback):
    _answer_callback(callback.get("id"))
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    data = str(callback.get("data") or "")

    if data == "trade:list":
        send(chat_id, _trade_list())
        return

    if data == "trade:menu":
        _trade_menu(chat_id)
        return

    if data == "trade:close":
        _close_menu(chat_id)
        return

    if data == "trade:reset":
        _trade_reset_menu(chat_id)
        return

    if data == "trade:reset_confirm":
        _trade_reset(chat_id)
        return

    if data.startswith("close:"):
        try:
            trade_id = int(data.split(":", 1)[1])
        except Exception:
            send(chat_id, "Не удалось определить сделку.")
            return
        _close_position_prompt(chat_id, trade_id)
        return

    if data in ("trade:LONG", "trade:SHORT"):
        side = data.split(":", 1)[1]
        _draft_set(chat_id, side=side, step=1, close_mode=False)
        send(
            chat_id,
            ("🟢 Открытие покупки / LONG\n\n" if side == "LONG"
             else "🔴 Открытие продажи / SHORT\n\n")
            + _trade_prompt(1)
            + "\n\nДля отмены: /cancel",
        )


def _handle_trade_form(chat_id, text):
    draft = _draft_get(chat_id)
    if not draft:
        return False

    if text.lower() in ("/cancel", "cancel", "отмена"):
        _draft_clear(chat_id)
        send(chat_id, "Заполнение сделки отменено.")
        return True

    if draft.get("close_mode"):
        step = int(draft.get("step", 1))

        if step == 1:
            value = _trade_num(text)
            if value is None:
                send(chat_id, "Нужно число. Например: 289.40")
                return True
            _draft_set(chat_id, close_price=value, step=2)
            send(
                chat_id,
                "Шаг 2/2 — комментарий к закрытию.\n"
                "Если комментария нет — напиши «нет».",
            )
            return True

        if step == 2:
            _draft_set(chat_id, close_notes=text.strip() or "нет", step=3)
            draft = _draft_get(chat_id)
            try:
                position = _close_trade(chat_id, draft)
                _draft_clear(chat_id)
                send(chat_id, _close_saved_text(position))
            except Exception as exc:
                _draft_clear(chat_id)
                print(f"TRADE CLOSE ERROR: {type(exc).__name__}: {exc}", flush=True)
                send(chat_id, f"Ошибка закрытия позиции: {type(exc).__name__}: {exc}")
            return True

    step = int(draft.get("step", 1))

    if step == 1:
        symbol = text.strip().upper().replace(" ", "")
        if len(symbol) < 2:
            send(chat_id, "Не удалось распознать инструмент. Повтори тикер.")
            return True
        _draft_set(chat_id, symbol=symbol, step=2)

    elif step in (2, 3, 4, 5, 6, 7, 8):
        value = _trade_num(text)
        if value is None:
            send(chat_id, "Нужно число. Например: 285.40")
            return True

        field = {
            2: "entry",
            3: "sl",
            4: "tp1",
            5: "tp2",
            6: "tp3",
            7: "quantity",
            8: "risk_rub",
        }[step]
        _draft_set(chat_id, **{field: value}, step=step + 1)

    elif step == 9:
        _draft_set(chat_id, notes=text.strip() or "нет", step=10)

    draft = _draft_get(chat_id)

    if draft.get("step") == 10:
        if draft.get("entry") is None or draft.get("sl") is None:
            _draft_clear(chat_id)
            send(chat_id, "Не удалось записать: отсутствует вход или SL.")
            return True

        if (
            draft["side"] == "LONG" and draft["sl"] >= draft["entry"]
        ) or (
            draft["side"] == "SHORT" and draft["sl"] <= draft["entry"]
        ):
            send(
                chat_id,
                "⚠️ SL расположен неправильно для выбранного направления.\n"
                "Сделка не записана. Начни заново через /trade.",
            )
            _draft_clear(chat_id)
            return True

        try:
            trade_id, obs, rr = _create_trade(chat_id, draft)
            _draft_clear(chat_id)
            send(chat_id, _trade_saved_text(trade_id, draft, obs, rr))
        except Exception as exc:
            _draft_clear(chat_id)
            print(f"TRADE SAVE ERROR: {type(exc).__name__}: {exc}", flush=True)
            send(chat_id, f"Ошибка записи сделки: {type(exc).__name__}: {exc}")
        return True

    send(chat_id, _trade_prompt(int(draft["step"])))
    return True


def _help_text():
    return (
        "🤖 Мой аналитик\n\n"
        "Команды:\n"
        "/scan — запустить сканирование\n"
        "/scanstatus — состояние сканера\n"
        "/market — состояние рынка MOEX\n"
        "/trade — журнал и ввод сделки\n"
        "/close — закрыть открытую позицию\n"
        "/report — отчёт по журналу сделок\n"
        "/status — состояние бота\n"
        "/cancel — отменить текущий ввод\n\n"
        "Важное: бот только фиксирует сделки и не отправляет заявки брокеру."
    )


def _status_text():
    with STATE_LOCK:
        running = SCAN_RUNNING
        start = LAST_SCAN_START
        finish = LAST_SCAN_FINISH
        error = LAST_SCAN_ERROR
    return (
        "Мой аналитик: OK\n"
        f"Сканер: {'RUNNING' if running else 'IDLE'}\n"
        "Webhook handler: OK\n"
        f"Последний старт: {_fmt_dt(start)}\n"
        f"Последнее завершение: {_fmt_dt(finish)}"
        + (f"\nПоследняя ошибка: {error}" if error else "")
    )


# ---------------------------------------------------------------------------
# Unified report: autonomous AI signals + real trades
# ---------------------------------------------------------------------------

def _ai_signal_rows():
    """
    Reads autonomous signal lifecycles from permanent Supabase/PostgreSQL.
    Falls back to the local SQLite journal if PostgreSQL is unavailable.

    active_signals is used instead of observations so repeated scans of the
    same signal are not counted as separate AI signals.
    """
    wanted = [
        "id", "created_ts", "updated_ts", "symbol", "market", "side",
        "lifecycle_status", "entry", "trigger", "sl", "tp1", "tp2", "tp3",
        "rr", "score", "price", "regime", "h1", "m15", "m5", "rsi",
        "volume_ratio", "reason", "tp1_hit_ts", "tp2_hit_ts",
        "tp3_hit_ts", "sl_hit_ts", "closed_ts", "close_price",
        "close_reason", "max_favorable_r", "max_adverse_r",
        "tp1_hit", "tp2_hit", "tp3_hit", "source_observation_ts",
        "signal_id",
    ]

    rows = []
    source = "SQLite"

    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        try:
            import psycopg

            conn = psycopg.connect(database_url, connect_timeout=10)
            try:
                query = """
                    SELECT
                        id, created_ts, updated_ts, symbol, market, side,
                        lifecycle_status, entry, trigger, sl, tp1, tp2, tp3,
                        rr, score, price, regime, h1, m15, m5, rsi,
                        volume_ratio, reason, tp1_hit_ts, tp2_hit_ts,
                        tp3_hit_ts, sl_hit_ts, closed_ts, close_price,
                        close_reason, max_favorable_r, max_adverse_r,
                        tp1_hit, tp2_hit, tp3_hit, source_observation_ts,
                        signal_id
                    FROM active_signals
                    ORDER BY created_ts DESC
                """
                rows = conn.execute(query).fetchall()
            finally:
                conn.close()

            source = "Supabase"
        except Exception as exc:
            print(
                f"AI REPORT PG ERROR: {type(exc).__name__}: {exc}",
                flush=True,
            )

    if not rows:
        try:
            con = _trade_db()
            try:
                existing = {
                    row[1] for row in con.execute("PRAGMA table_info(active_signals)")
                }

                if existing:
                    selected = [column for column in wanted if column in existing]
                    if "id" in selected and "symbol" in selected:
                        rows = con.execute(
                            "SELECT " + ",".join(selected) +
                            " FROM active_signals ORDER BY created_ts DESC"
                        ).fetchall()

                        normalized = []
                        for row in rows:
                            data = dict(zip(selected, row))
                            normalized.append(tuple(data.get(c) for c in wanted))
                        rows = normalized
            finally:
                con.close()
        except Exception as exc:
            print(
                f"AI REPORT SQLITE ERROR: {type(exc).__name__}: {exc}",
                flush=True,
            )

    return rows, source


def _ai_signal_result(row):
    """
    Returns:
      open  -> signal is still being tracked
      win   -> terminal TP3
      loss  -> terminal SL
      closed -> manually/administratively closed without TP3/SL
    """
    status = str(row[6] or "").upper()
    side = str(row[5] or "").upper()
    entry = _trade_num(row[7])
    sl = _trade_num(row[9])
    close_price = _trade_num(row[28])

    if status in ("ACTIVE", "TP1", "TP2", "WATCH"):
        return "open", None

    if status == "TP3":
        if (
            entry is not None
            and sl is not None
            and close_price is not None
            and abs(entry - sl) > 0
        ):
            if side == "LONG":
                return "win", (close_price - entry) / abs(entry - sl)
            return "win", (entry - close_price) / abs(entry - sl)
        return "win", None

    if status == "SL":
        return "loss", -1.0

    # A manual/administrative close is terminal, but is not a win/loss.
    if row[27] or row[29] or status:
        return "closed", None

    return "open", None


def _fmt_msk(value):
    if not value:
        return "—"

    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        msk = timezone(timedelta(hours=3), "MSK")
        return value.astimezone(msk).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def _fmt_num(value, digits=3):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _journal_report():
    # ----- Autonomous AI signals -----
    ai_rows, ai_source = _ai_signal_rows()

    ai_total = len(ai_rows)
    ai_open = 0
    ai_closed = 0
    ai_wins = 0
    ai_losses = 0
    ai_no_result = 0
    ai_longs = 0
    ai_shorts = 0
    ai_rs = []

    tp1_hits = 0
    tp2_hits = 0
    tp3_hits = 0
    sl_hits = 0

    for row in ai_rows:
        side = str(row[5] or "").upper()

        if side == "LONG":
            ai_longs += 1
        elif side == "SHORT":
            ai_shorts += 1

        state, result_r = _ai_signal_result(row)

        if state == "open":
            ai_open += 1
        elif state in ("win", "loss", "closed"):
            ai_closed += 1

            if state == "win":
                ai_wins += 1
            elif state == "loss":
                ai_losses += 1
            else:
                ai_no_result += 1

        if result_r is not None:
            ai_rs.append(float(result_r))

        # Infer terminal TP3/SL from lifecycle as a compatibility fallback.
        if bool(row[33]) or row[23]:
            tp1_hits += 1
        if bool(row[34]) or row[24]:
            tp2_hits += 1
        if bool(row[35]) or str(row[6] or "").upper() == "TP3":
            tp3_hits += 1
        if bool(row[32]) or str(row[6] or "").upper() == "SL":
            sl_hits += 1

    ai_avg = sum(ai_rs) / len(ai_rs) if ai_rs else None
    ai_sum = sum(ai_rs) if ai_rs else None

    # ----- Real trades -----
    con = _trade_db()
    try:
        total = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        opened = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN'"
        ).fetchone()[0]
        closed = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status='CLOSED'"
        ).fetchone()[0]
        wins = con.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE status='CLOSED' AND result_r > 0"
        ).fetchone()[0]
        losses = con.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE status='CLOSED' AND result_r < 0"
        ).fetchone()[0]
        breakeven = con.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE status='CLOSED' AND result_r = 0"
        ).fetchone()[0]
        avg_r = con.execute(
            "SELECT AVG(result_r) FROM trades "
            "WHERE status='CLOSED' AND result_r IS NOT NULL"
        ).fetchone()[0]
        sum_r = con.execute(
            "SELECT SUM(result_r) FROM trades "
            "WHERE status='CLOSED' AND result_r IS NOT NULL"
        ).fetchone()[0]
        longs = con.execute(
            "SELECT COUNT(*) FROM trades WHERE side='LONG'"
        ).fetchone()[0]
        shorts = con.execute(
            "SELECT COUNT(*) FROM trades WHERE side='SHORT'"
        ).fetchone()[0]
        latest = con.execute(
            """SELECT id,created_ts,symbol,side,entry,sl,tp1,tp2,tp3,
                      status,close_price,result_r,closed_ts
               FROM trades ORDER BY id DESC LIMIT 5"""
        ).fetchall()
    finally:
        con.close()

    lines = [
        "📊 ОТЧЁТ ТОРГОВОГО ИИ",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🤖 АВТОНОМНЫЕ СИГНАЛЫ ИИ",
        "Сигналы, найденные системой самостоятельно",
        f"Источник журнала: {ai_source}",
        "",
        f"Всего сигналов: {ai_total}",
        f"Открытых: {ai_open}",
        f"Закрытых: {ai_closed}",
        "",
        f"LONG: {ai_longs}",
        f"SHORT: {ai_shorts}",
        "",
        f"Побед: {ai_wins}",
        f"Убытков: {ai_losses}",
        f"Без результата: {ai_no_result}",
        (
            f"Win rate: {(ai_wins / (ai_wins + ai_losses) * 100):.1f}%"
            if (ai_wins + ai_losses)
            else "Win rate: —"
        ),
        (
            f"Средний результат: {ai_avg:+.2f} R"
            if ai_avg is not None
            else "Средний результат: —"
        ),
        (
            f"Суммарный результат: {ai_sum:+.2f} R"
            if ai_sum is not None
            else "Суммарный результат: —"
        ),
        "",
        f"TP1: {tp1_hits}",
        f"TP2: {tp2_hits}",
        f"TP3: {tp3_hits}",
        f"SL: {sl_hits}",
        "",
        "Последние AI-сигналы:",
    ]

    if not ai_rows:
        lines.append("— автономных сигналов пока нет")
    else:
        for row in ai_rows[:5]:
            symbol = row[3] or "?"
            side = str(row[5] or "?").upper()
            state, result_r = _ai_signal_result(row)

            if state == "open":
                status_text = "🟡 В РАБОТЕ"
                result_text = "результат пока не зафиксирован"
            elif state == "win":
                status_text = "🟢 TP3"
                result_text = (
                    f"результат: {result_r:+.2f} R"
                    if result_r is not None
                    else "результат: TP3"
                )
            elif state == "loss":
                status_text = "🔴 SL"
                result_text = "результат: -1.00 R"
            else:
                status_text = "⚪ ЗАКРЫТ"
                result_text = "результат не классифицирован"

            lines += [
                "",
                f"{symbol} {side}",
                f"Время: {_fmt_msk(row[1])} MSK",
                f"Score: {_fmt_num(row[14], 1)}",
                f"Вход: {_fmt_num(row[7])}",
                f"Trigger: {_fmt_num(row[8])}",
                f"SL: {_fmt_num(row[9])}",
                f"TP1: {_fmt_num(row[10])}",
                f"TP2: {_fmt_num(row[11])}",
                f"TP3: {_fmt_num(row[12])}",
                f"RR: {_fmt_num(row[13], 2)}",
                f"Статус: {status_text}",
                f"Результат: {result_text}",
            ]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "💼 РЕАЛЬНЫЕ СДЕЛКИ",
        "Позиции, которые пользователь действительно открыл",
        "",
        f"Всего сделок: {total}",
        f"Открытых: {opened}",
        f"Закрытых: {closed}",
        f"Прибыльных: {wins}",
        f"Убыточных: {losses}",
        f"Без результата: {breakeven}",
        f"LONG: {longs} | SHORT: {shorts}",
        (
            f"Win rate: {(wins / (wins + losses) * 100):.1f}%"
            if (wins + losses)
            else "Win rate: —"
        ),
        (
            f"Средний результат: {avg_r:+.2f} R"
            if avg_r is not None
            else "Средний результат: —"
        ),
        (
            f"Суммарный результат: {sum_r:+.2f} R"
            if sum_r is not None
            else "Суммарный результат: —"
        ),
        "",
        "Последние реальные сделки:",
    ]

    if not latest:
        lines.append("— реальных сделок пока нет")
    else:
        for (
            tid, created_ts, symbol, side, entry, sl, tp1, tp2, tp3,
            status, close_price, result_r, closed_ts
        ) in latest:
            result = f" | {result_r:+.2f} R" if result_r is not None else ""
            close = f" → закрытие {close_price}" if close_price is not None else ""
            lines.append(
                f"#{tid} {symbol} {side} | вход {entry} | SL {sl}"
                f"{close} | {status}{result}"
            )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 ОБЩАЯ СТАТИСТИКА",
        f"Реальные сделки: {total}",
        f"Автономные сигналы ИИ: {ai_total}",
        (
            f"Реальный Win rate: {(wins / (wins + losses) * 100):.1f}%"
            if (wins + losses)
            else "Реальный Win rate: —"
        ),
        (
            f"AI Win rate: {(ai_wins / (ai_wins + ai_losses) * 100):.1f}%"
            if (ai_wins + ai_losses)
            else "AI Win rate: —"
        ),
        (
            f"Реальный результат: {sum_r:+.2f} R"
            if sum_r is not None
            else "Реальный результат: —"
        ),
        (
            f"AI результат: {ai_sum:+.2f} R"
            if ai_sum is not None
            else "AI результат: —"
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🧠 ПРОТОКОЛ",
        "AI-сигналы учитываются отдельно от реальных сделок.",
        "Они участвуют в AI Win Rate и статистике R.",
        f"Закрытых AI-сигналов с классифицируемым исходом: {ai_wins + ai_losses}.",
        "По накопленной статистике можно отдельно анализировать и корректировать торговый протокол.",
    ]

    return "\n".join(lines)


def _trade_reset_menu(chat_id):
    return _send_markup(
        chat_id,
        "🧹 Очистка журнала сделок\n\n"
        "Это удалит все записи из таблицы реальных сделок бота.\n"
        "Наблюдения сканера НЕ удаляются.\n\n"
        "Подтвердить очистку?",
        {
            "inline_keyboard": [
                [{"text": "🗑 Удалить сделки", "callback_data": "trade:reset_confirm"}],
                [{"text": "Отмена", "callback_data": "trade:menu"}],
            ]
        },
    )


def _trade_reset(chat_id):
    con = _trade_db()
    try:
        count = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        con.execute("DELETE FROM trades")
        con.execute("DELETE FROM sqlite_sequence WHERE name='trades'")
        con.commit()
    finally:
        con.close()
    send(
        chat_id,
        f"🧹 Журнал сделок очищен. Удалено записей: {count}.\n"
        "Наблюдения сканера сохранены.",
    )


def handle(update):
    global LAST_CHAT_ID

    if not isinstance(update, dict):
        return

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is not None:
            LAST_CHAT_ID = chat_id
        _handle_trade_callback(callback)
        return

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if chat_id is not None:
        LAST_CHAT_ID = chat_id

    text = (message.get("text") or "").strip()
    if not text:
        return

    if _handle_trade_form(chat_id, text):
        return

    command = text.split()[0].lower().split("@", 1)[0]

    if command in ("/cancel", "cancel", "отмена"):
        _draft_clear(chat_id)
        send(chat_id, "Активное заполнение сделки отсутствует.")
        return

    if command in ("/start", "/help", "help", "/помощь", "помощь"):
        send(chat_id, _help_text())
        return

    if command in ("/trade", "trade", "/сделка", "сделка"):
        _trade_menu(chat_id)
        return

    if command in ("/close", "close", "/закрыть", "закрыть"):
        _close_menu(chat_id)
        return

    if command in ("/tradereset", "tradereset"):
        _trade_reset_menu(chat_id)
        return

    if command in ("/market", "market", "рынок"):
        send(chat_id, market_text())
        return

    if command in ("/scanstatus", "scanstatus", "статусскана"):
        send(chat_id, scan_status_text())
        return

    if command in ("/status", "status"):
        send(chat_id, _status_text())
        return

    if command in ("/report", "report", "отчет", "отчёт"):
        send(chat_id, _journal_report())
        return

    if command in ("/scan", "scan", "скан", "сканирование"):
        if enqueue_scan(chat_id):
            send(
                chat_id,
                "Сканирование запущено в фоне.\n"
                "После завершения результат придёт отдельным сообщением.",
            )
        else:
            send(chat_id, "Сканирование уже выполняется.")
        return

    send(chat_id, "Неизвестная команда.\n\n" + _help_text())


def bot_info():
    if not TELEGRAM_BOT_TOKEN:
        return {
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN is not set",
        }

    try:
        response = requests.get(
            f"{TELEGRAM_API}/getMe",
            timeout=10,
        )

        response.raise_for_status()
        return response.json()

    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
