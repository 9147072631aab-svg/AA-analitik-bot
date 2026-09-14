import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.getenv("AA_JOURNAL_DB", "/tmp/aa_analitik_journal.sqlite3")
LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT,
    side TEXT,
    status TEXT,
    score REAL,
    price REAL,
    entry REAL,
    trigger REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    rr REAL,
    regime TEXT,
    h1 TEXT,
    m15 TEXT,
    m5 TEXT,
    rsi REAL,
    volume_ratio REAL,
    trigger_distance_atr REAL,
    reason TEXT,
    watch INTEGER DEFAULT 0,
    outcome_status TEXT DEFAULT 'OPEN',
    outcome_r REAL,
    max_favorable_r REAL DEFAULT 0,
    max_adverse_r REAL DEFAULT 0,
    closed_ts TEXT,
    UNIQUE(ts, symbol, side)
);

CREATE INDEX IF NOT EXISTS idx_obs_symbol_side_ts
ON observations(symbol, side, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT,
    side TEXT,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    old_score REAL,
    new_score REAL,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _n(v):
    try:
        return float(v)
    except Exception:
        return None


def _key(x):
    return (str(x.get("symbol", "")), str(x.get("side", "")))


def _last(con, symbol, side):
    return con.execute(
        """SELECT * FROM observations
           WHERE symbol=? AND side=?
           ORDER BY id DESC LIMIT 1""",
        (symbol, side),
    ).fetchone()


def _columns(con):
    return [r[1] for r in con.execute("PRAGMA table_info(observations)")]


def save_scan(result):
    """Persist every scan and return event notifications only.

    Telegram stays quiet unless a meaningful state transition occurs.
    """
    ts = now()
    events = []
    with LOCK:
        con = connect()
        try:
            for market_key in ("stocks", "futures"):
                market = "TQBR" if market_key == "stocks" else "FORTS"
                for x in result.get(market_key, []) or []:
                    symbol, side = _key(x)
                    if not symbol or side not in ("LONG", "SHORT", "WAIT"):
                        continue

                    previous = _last(con, symbol, side)
                    old_status = previous[5] if previous else None
                    old_score = previous[6] if previous else None
                    status = x.get("status", "WAIT")
                    score = _n(x.get("score"))
                    watch = 1 if x.get("watch") else 0

                    con.execute(
                        """INSERT OR IGNORE INTO observations
                        (ts,symbol,market,side,status,score,price,entry,trigger,sl,
                         tp1,tp2,tp3,rr,regime,h1,m15,m5,rsi,volume_ratio,
                         trigger_distance_atr,reason,watch)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            ts, symbol, market, side, status, score, _n(x.get("price")),
                            _n(x.get("entry")), _n(x.get("trigger")), _n(x.get("sl")),
                            _n(x.get("tp1")), _n(x.get("tp2")), _n(x.get("tp3")),
                            _n(x.get("rr")), x.get("regime"), x.get("h1"), x.get("m15"),
                            x.get("m5"), _n(x.get("rsi")), _n(x.get("volume_ratio")),
                            _n(x.get("trigger_distance_atr")), str(x.get("reason", ""))[:1000],
                            watch,
                        ),
                    )

                    # Meaningful Telegram transitions.
                    if previous:
                        if old_status != status and status in ("LONG", "SHORT"):
                            events.append({
                                "type": "ENTRY_READY",
                                "symbol": symbol,
                                "side": side,
                                "message": f"{symbol} {side}: ENTRY READY",
                                "score": score,
                            })
                        elif old_status in ("LONG", "SHORT") and status == "WAIT":
                            events.append({
                                "type": "INVALIDATED",
                                "symbol": symbol,
                                "side": side,
                                "message": f"{symbol} {side}: setup invalidated",
                                "score": score,
                            })
                        elif old_score is not None and score is not None and abs(score - old_score) >= 10:
                            direction = "усилен" if score > old_score else "ослаблен"
                            events.append({
                                "type": "SCORE_SHIFT",
                                "symbol": symbol,
                                "side": side,
                                "message": f"{symbol} {side}: score {old_score:.0f} → {score:.0f} ({direction})",
                                "score": score,
                            })

            con.commit()
        finally:
            con.close()

    return events


def _risk(entry, sl):
    if entry is None or sl is None:
        return None
    return abs(entry - sl)


def update_virtual_outcomes(result):
    """Update hypothetical confirmed setups using the latest observed price.

    This is a first-pass observation engine: it evaluates the price seen at each
    scan. It does not claim intrabar execution accuracy.
    """
    ts = now()
    with LOCK:
        con = connect()
        try:
            rows = con.execute(
                """SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,outcome_status,
                          max_favorable_r,max_adverse_r
                   FROM observations
                   WHERE status IN ('LONG','SHORT')
                     AND outcome_status='OPEN'
                   ORDER BY id"""
            ).fetchall()

            prices = {}
            for key in ("stocks", "futures"):
                for x in result.get(key, []) or []:
                    prices[(str(x.get("symbol","")), str(x.get("side","")))] = _n(x.get("price"))

            for row in rows:
                oid, symbol, side, entry, sl, tp1, tp2, tp3, state, mfe, mae = row
                price = prices.get((symbol, side))
                risk = _risk(entry, sl)
                if price is None or entry is None or risk in (None, 0):
                    continue

                if side == "LONG":
                    favorable = (price - entry) / risk
                    adverse = (entry - price) / risk
                    hit_sl = price <= sl if sl is not None else False
                    hit_tp3 = price >= tp3 if tp3 is not None else False
                    hit_tp2 = price >= tp2 if tp2 is not None else False
                    hit_tp1 = price >= tp1 if tp1 is not None else False
                else:
                    favorable = (entry - price) / risk
                    adverse = (price - entry) / risk
                    hit_sl = price >= sl if sl is not None else False
                    hit_tp3 = price <= tp3 if tp3 is not None else False
                    hit_tp2 = price <= tp2 if tp2 is not None else False
                    hit_tp1 = price <= tp1 if tp1 is not None else False

                new_mfe = max(float(mfe or 0), favorable)
                new_mae = max(float(mae or 0), adverse)

                close_state = None
                outcome_r = None
                if hit_sl:
                    close_state, outcome_r = "SL", -1.0
                elif hit_tp3:
                    close_state, outcome_r = "TP3", (float(tp3)-float(entry))/risk if side == "LONG" else (float(entry)-float(tp3))/risk
                elif hit_tp2:
                    close_state, outcome_r = "TP2", (float(tp2)-float(entry))/risk if side == "LONG" else (float(entry)-float(tp2))/risk
                elif hit_tp1:
                    close_state, outcome_r = "TP1", (float(tp1)-float(entry))/risk if side == "LONG" else (float(entry)-float(tp1))/risk

                if close_state:
                    con.execute(
                        """UPDATE observations
                           SET outcome_status=?, outcome_r=?, max_favorable_r=?,
                               max_adverse_r=?, closed_ts=?
                           WHERE id=?""",
                        (close_state, outcome_r, new_mfe, new_mae, ts, oid),
                    )
                else:
                    con.execute(
                        """UPDATE observations
                           SET max_favorable_r=?, max_adverse_r=?
                           WHERE id=?""",
                        (new_mfe, new_mae, oid),
                    )
            con.commit()
        finally:
            con.close()


def daily_stats():
    with LOCK:
        con = connect()
        try:
            total = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            ready = con.execute(
                "SELECT COUNT(*) FROM observations WHERE status IN ('LONG','SHORT')"
            ).fetchone()[0]
            closed = con.execute(
                "SELECT COUNT(*) FROM observations WHERE outcome_status != 'OPEN'"
            ).fetchone()[0]
            wins = con.execute(
                "SELECT COUNT(*) FROM observations WHERE outcome_status IN ('TP1','TP2','TP3')"
            ).fetchone()[0]
            avg_r = con.execute(
                "SELECT AVG(outcome_r) FROM observations WHERE outcome_r IS NOT NULL"
            ).fetchone()[0]
            return {
                "observations": total,
                "ready": ready,
                "closed": closed,
                "wins": wins,
                "win_rate": (wins / closed * 100) if closed else None,
                "avg_r": avg_r,
            }
        finally:
            con.close()
