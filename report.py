# report_patch.py
# Готовый модуль отчёта для AA-Analitik-bot.
# Он собирает в одном отчёте:
# 1) реальные сделки из SQLite таблицы trades;
# 2) автономные AI-сигналы из таблицы active_signals;
# 3) отдельную статистику AI.
#
# В bot.py достаточно заменить старую _journal_report() на:
# from report_patch import build_report
# ...
# return build_report()

import os
import sqlite3

DB_PATH = os.getenv("AA_JOURNAL_DB", "/tmp/aa_analitik_journal.sqlite3")


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _pct(wins, closed):
    return f"{wins / closed * 100:.1f}%" if closed else "—"


def build_report():
    con = _db()
    try:
        # ---------- REAL TRADES ----------
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

        latest_real = con.execute(
            "SELECT id,symbol,side,entry,close_price,result_r,status "
            "FROM trades ORDER BY id DESC LIMIT 5"
        ).fetchall()

        # ---------- AI SIGNALS ----------
        # active_signals contains one logical signal per symbol/side,
        # including closed signals. Repeated scan observations are not
        # counted as separate signals.
        ai_rows = con.execute(
            """SELECT signal_id,symbol,side,created_ts,entry,sl,tp1,tp2,tp3,
                      rr,score,price,lifecycle_status,close_price,
                      close_reason,max_favorable_r,max_adverse_r
               FROM active_signals
               ORDER BY id DESC"""
        ).fetchall()

        ai_total = len(ai_rows)
        ai_open = sum(
            1 for r in ai_rows
            if r[12] in ("ACTIVE", "TP1", "TP2")
        )
        ai_closed = ai_total - ai_open

        ai_wins = sum(
            1 for r in ai_rows
            if r[12] == "TP3"
        )
        ai_losses = sum(
            1 for r in ai_rows
            if r[12] == "SL"
        )
        ai_other_closed = max(0, ai_closed - ai_wins - ai_losses)

        ai_results = []
        for r in ai_rows:
            status = r[12]
            entry = r[4]
            sl = r[5]
            tp3 = r[8]

            if status == "SL":
                ai_results.append(-1.0)
            elif status == "TP3" and entry is not None and sl is not None and tp3 is not None:
                risk = abs(entry - sl)
                if risk > 0:
                    if r[2] == "LONG":
                        ai_results.append((tp3 - entry) / risk)
                    else:
                        ai_results.append((entry - tp3) / risk)

        ai_avg_r = sum(ai_results) / len(ai_results) if ai_results else None
        ai_sum_r = sum(ai_results) if ai_results else None

        ai_long = sum(1 for r in ai_rows if r[2] == "LONG")
        ai_short = sum(1 for r in ai_rows if r[2] == "SHORT")

        tp1_hits = sum(1 for r in ai_rows if r[12] in ("TP1", "TP2", "TP3"))
        tp2_hits = sum(1 for r in ai_rows if r[12] in ("TP2", "TP3"))
        tp3_hits = ai_wins
        sl_hits = ai_losses

        # ---------- REPORT ----------
        lines = [
            "📊 ОТЧЁТ ТОРГОВОГО ИИ",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            "🤖 АВТОНОМНЫЕ СИГНАЛЫ ИИ",
            "Сигналы, найденные системой самостоятельно",
            "",
            f"Всего сигналов: {ai_total}",
            f"Открытых: {ai_open}",
            f"Закрытых: {ai_closed}",
            "",
            f"LONG: {ai_long}",
            f"SHORT: {ai_short}",
            "",
            f"Побед: {ai_wins}",
            f"Убытков: {ai_losses}",
            f"Без результата: {ai_other_closed}",
            f"Win rate: {_pct(ai_wins, ai_closed)}",
            f"Средний результат: {_fmt(ai_avg_r) + ' R' if ai_avg_r is not None else '—'}",
            f"Суммарный результат: {_fmt(ai_sum_r) + ' R' if ai_sum_r is not None else '—'}",
            "",
            f"TP1: {tp1_hits}",
            f"TP2: {tp2_hits}",
            f"TP3: {tp3_hits}",
            f"SL: {sl_hits}",
            "",
            "Последние AI-сигналы:",
        ]

        if not ai_rows:
            lines.append("— сигналов пока нет")
        else:
            for r in ai_rows[:5]:
                signal_id, symbol, side, created_ts, entry, sl, tp1, tp2, tp3, rr, score, price, status, close_price, close_reason, mfe, mae = r
                if status in ("ACTIVE", "TP1", "TP2"):
                    status_text = "🟡 В РАБОТЕ"
                    result_text = "результат пока не зафиксирован"
                elif status == "TP3":
                    status_text = "🟢 ЗАКРЫТ"
                    result_text = "TP3"
                elif status == "SL":
                    status_text = "🔴 ЗАКРЫТ"
                    result_text = "SL"
                else:
                    status_text = f"⚪ {status}"
                    result_text = close_reason or status

                lines += [
                    "",
                    f"{'🟢' if side == 'LONG' else '🔴'} {symbol} {side}",
                    f"Время: {created_ts}",
                    f"Score: {_fmt(score)}",
                    f"Вход: {_fmt(entry)}",
                    f"SL: {_fmt(sl)}",
                    f"TP1: {_fmt(tp1)}",
                    f"TP2: {_fmt(tp2)}",
                    f"TP3: {_fmt(tp3)}",
                    f"RR: {_fmt(rr)}",
                    f"Статус: {status_text}",
                    f"Результат: {result_text}",
                ]

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            "💼 РЕАЛЬНЫЕ СДЕЛКИ",
            "Только позиции, которые действительно были введены пользователем",
            "",
            f"Всего сделок: {total}",
            f"Открытых: {opened}",
            f"Закрытых: {closed}",
            f"Прибыльных: {wins}",
            f"Убыточных: {losses}",
            f"Без результата: {breakeven}",
            f"LONG: {longs} | SHORT: {shorts}",
            f"Win rate: {_pct(wins, closed)}",
            f"Средний результат: {_fmt(avg_r) + ' R' if avg_r is not None else '—'}",
            f"Суммарный результат: {_fmt(sum_r) + ' R' if sum_r is not None else '—'}",
            "",
            "Последние сделки:",
        ]

        if not latest_real:
            lines.append("— сделок пока нет")
        else:
            for tid, symbol, side, entry, close_price, result_r, status in latest_real:
                result = f" | {result_r:+.2f} R" if result_r is not None else ""
                close = f" → {close_price}" if close_price is not None else ""
                lines.append(
                    f"#{tid} {symbol} {side}: {entry}{close} | {status}{result}"
                )

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            "📊 ОБЩАЯ СТАТИСТИКА",
            "",
            f"Реальные сделки: {total}",
            f"Автономные сигналы ИИ: {ai_total}",
            "",
            f"Реальный Win rate: {_pct(wins, closed)}",
            f"AI Win rate: {_pct(ai_wins, ai_closed)}",
            "",
            f"Реальный результат: {_fmt(sum_r) + ' R' if sum_r is not None else '—'}",
            f"AI результат: {_fmt(ai_sum_r) + ' R' if ai_sum_r is not None else '—'}",
            "",
            "🧠 ПРОТОКОЛ",
            "AI-сигналы учитываются отдельно от реальных сделок.",
            "Они участвуют в AI Win Rate и статистике R.",
            "На накопленной статистике можно корректировать торговый протокол.",
        ]

        return "\n".join(lines)
    finally:
        con.close()


if __name__ == "__main__":
    print(build_report())
