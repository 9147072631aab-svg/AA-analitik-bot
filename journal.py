import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.getenv("AA_JOURNAL_DB", "/tmp/aa_analitik_journal.sqlite3")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
CREATE INDEX IF NOT EXISTS idx_obs_symbol_side_ts ON observations(symbol, side, ts);

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

CREATE TABLE IF NOT EXISTS active_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL UNIQUE,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    market TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    trigger REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    rr REAL,
    score REAL,
    price REAL,
    regime TEXT,
    h1 TEXT,
    m15 TEXT,
    m5 TEXT,
    rsi REAL,
    volume_ratio REAL,
    trigger_distance_atr REAL,
    reason TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    tp1_hit INTEGER NOT NULL DEFAULT 0,
    tp2_hit INTEGER NOT NULL DEFAULT 0,
    max_favorable_r REAL NOT NULL DEFAULT 0,
    max_adverse_r REAL NOT NULL DEFAULT 0,
    closed_ts TEXT,
    close_price REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_active_signals_symbol_side ON active_signals(symbol, side, lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_active_signals_status ON active_signals(lifecycle_status);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT,
    side TEXT,
    status TEXT,
    score DOUBLE PRECISION,
    price DOUBLE PRECISION,
    entry DOUBLE PRECISION,
    trigger DOUBLE PRECISION,
    sl DOUBLE PRECISION,
    tp1 DOUBLE PRECISION,
    tp2 DOUBLE PRECISION,
    tp3 DOUBLE PRECISION,
    rr DOUBLE PRECISION,
    regime TEXT,
    h1 TEXT,
    m15 TEXT,
    m5 TEXT,
    rsi DOUBLE PRECISION,
    volume_ratio DOUBLE PRECISION,
    trigger_distance_atr DOUBLE PRECISION,
    reason TEXT,
    watch INTEGER DEFAULT 0,
    outcome_status TEXT DEFAULT 'OPEN',
    outcome_r DOUBLE PRECISION,
    max_favorable_r DOUBLE PRECISION DEFAULT 0,
    max_adverse_r DOUBLE PRECISION DEFAULT 0,
    closed_ts TEXT,
    UNIQUE(ts, symbol, side)
);
CREATE INDEX IF NOT EXISTS idx_pg_obs_symbol_side_ts ON observations(symbol, side, ts);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT,
    side TEXT,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    old_score DOUBLE PRECISION,
    new_score DOUBLE PRECISION,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_pg_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS active_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    market TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry DOUBLE PRECISION NOT NULL,
    trigger DOUBLE PRECISION,
    sl DOUBLE PRECISION,
    tp1 DOUBLE PRECISION,
    tp2 DOUBLE PRECISION,
    tp3 DOUBLE PRECISION,
    rr DOUBLE PRECISION,
    score DOUBLE PRECISION,
    price DOUBLE PRECISION,
    regime TEXT,
    h1 TEXT,
    m15 TEXT,
    m5 TEXT,
    rsi DOUBLE PRECISION,
    volume_ratio DOUBLE PRECISION,
    trigger_distance_atr DOUBLE PRECISION,
    reason TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    tp1_hit INTEGER NOT NULL DEFAULT 0,
    tp2_hit INTEGER NOT NULL DEFAULT 0,
    max_favorable_r DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_adverse_r DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed_ts TEXT,
    close_price DOUBLE PRECISION,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pg_active_symbol_side ON active_signals(symbol, side, lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_pg_active_status ON active_signals(lifecycle_status);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def _n(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _key(x):
    return str(x.get("symbol", "")).upper(), str(x.get("side", "")).upper()


def _risk(entry, sl):
    if entry is None or sl is None:
        return None
    return abs(float(entry) - float(sl))


def _pg_connect():
    if not DATABASE_URL:
        return None
    try:
        import psycopg
        con = psycopg.connect(DATABASE_URL, connect_timeout=8)
        with con.cursor() as cur:
            cur.execute(PG_SCHEMA)
        con.commit()
        return con
    except Exception as exc:
        print(f"POSTGRES JOURNAL ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _last(con, symbol, side):
    return con.execute(
        "SELECT * FROM observations WHERE symbol=? AND side=? ORDER BY id DESC LIMIT 1",
        (symbol, side),
    ).fetchone()


def _columns(con):
    return [r[1] for r in con.execute("PRAGMA table_info(observations)")]


def _signal_from_item(item, market, ts):
    symbol, side = _key(item)
    entry = _n(item.get("entry"))
    trigger = _n(item.get("trigger"))
    sl = _n(item.get("sl", item.get("stop")))
    tp1 = _n(item.get("tp1", item.get("take_profit_1")))
    tp2 = _n(item.get("tp2", item.get("take_profit_2")))
    tp3 = _n(item.get("tp3", item.get("take_profit_3")))
    if entry is None or side not in ("LONG", "SHORT"):
        return None
    return {
        "signal_id": uuid.uuid4().hex,
        "created_ts": ts,
        "updated_ts": ts,
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "trigger": trigger,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": _n(item.get("rr")),
        "score": _n(item.get("score")),
        "price": _n(item.get("price")),
        "regime": item.get("regime"),
        "h1": item.get("h1"),
        "m15": item.get("m15"),
        "m5": item.get("m5"),
        "rsi": _n(item.get("rsi")),
        "volume_ratio": _n(item.get("volume_ratio")),
        "trigger_distance_atr": _n(item.get("trigger_distance_atr")),
        "reason": str(item.get("reason", ""))[:2000],
    }


def _sqlite_upsert_active(con, signal):
    existing = con.execute(
        "SELECT id, signal_id FROM active_signals WHERE symbol=? AND side=? AND lifecycle_status='ACTIVE' ORDER BY id DESC LIMIT 1",
        (signal["symbol"], signal["side"]),
    ).fetchone()
    if existing:
        # Keep the original lifecycle and fixed levels. Only refresh the observed price/diagnostics.
        con.execute(
            """UPDATE active_signals SET updated_ts=?, price=?, score=?, regime=?, h1=?, m15=?, m5=?, rsi=?, volume_ratio=?, trigger_distance_atr=?, reason=? WHERE id=?""",
            (signal["updated_ts"], signal["price"], signal["score"], signal["regime"], signal["h1"], signal["m15"], signal["m5"], signal["rsi"], signal["volume_ratio"], signal["trigger_distance_atr"], signal["reason"], existing[0]),
        )
        return existing[1], False
    con.execute(
        """INSERT INTO active_signals
        (signal_id,created_ts,updated_ts,market,symbol,side,entry,trigger,sl,tp1,tp2,tp3,rr,score,price,regime,h1,m15,m5,rsi,volume_ratio,trigger_distance_atr,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(signal[k] for k in ("signal_id","created_ts","updated_ts","market","symbol","side","entry","trigger","sl","tp1","tp2","tp3","rr","score","price","regime","h1","m15","m5","rsi","volume_ratio","trigger_distance_atr","reason")),
    )
    return signal["signal_id"], True


def _pg_upsert_active(con, signal):
    with con.cursor() as cur:
        cur.execute(
            "SELECT signal_id FROM active_signals WHERE symbol=%s AND side=%s AND lifecycle_status='ACTIVE' ORDER BY id DESC LIMIT 1",
            (signal["symbol"], signal["side"]),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """UPDATE active_signals SET updated_ts=%s, price=%s, score=%s, regime=%s, h1=%s, m15=%s, m5=%s, rsi=%s, volume_ratio=%s, trigger_distance_atr=%s, reason=%s WHERE signal_id=%s""",
                (signal["updated_ts"], signal["price"], signal["score"], signal["regime"], signal["h1"], signal["m15"], signal["m5"], signal["rsi"], signal["volume_ratio"], signal["trigger_distance_atr"], signal["reason"], row[0]),
            )
            con.commit()
            return row[0], False
        cols = ("signal_id","created_ts","updated_ts","market","symbol","side","entry","trigger","sl","tp1","tp2","tp3","rr","score","price","regime","h1","m15","m5","rsi","volume_ratio","trigger_distance_atr","reason")
        placeholders = ",".join(["%s"] * len(cols))
        cur.execute(f"INSERT INTO active_signals ({','.join(cols)}) VALUES ({placeholders})", tuple(signal[k] for k in cols))
    con.commit()
    return signal["signal_id"], True


def _append_active(result, active_items):
    if not active_items:
        return
    result.setdefault("active_signals", [])
    result["active_signals"] = active_items


def save_scan(result):
    """Persist scans and create persistent ACTIVE signal lifecycles.

    Once a LONG/SHORT setup has entry/SL/TP levels and passes the scanner gate,
    its lifecycle is independent from later scanner status. A later WAIT does not
    delete or invalidate the active lifecycle; only SL, TP3, or explicit manual
    close ends it.
    """
    ts = now()
    events = []
    active_items = []
    with LOCK:
        con = connect()
        pg = _pg_connect()
        try:
            for market_key in ("stocks", "futures"):
                market = "TQBR" if market_key == "stocks" else "FORTS"
                for x in result.get(market_key, []) or []:
                    if not isinstance(x, dict):
                        continue
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
                        (ts,symbol,market,side,status,score,price,entry,trigger,sl,tp1,tp2,tp3,rr,regime,h1,m15,m5,rsi,volume_ratio,trigger_distance_atr,reason,watch)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ts, symbol, market, side, status, score, _n(x.get("price")), _n(x.get("entry")), _n(x.get("trigger")), _n(x.get("sl", x.get("stop"))), _n(x.get("tp1")), _n(x.get("tp2")), _n(x.get("tp3")), _n(x.get("rr")), x.get("regime"), x.get("h1"), x.get("m15"), x.get("m5"), _n(x.get("rsi")), _n(x.get("volume_ratio")), _n(x.get("trigger_distance_atr")), str(x.get("reason", ""))[:1000], watch),
                    )
                    if previous and old_status != status and status in ("LONG", "SHORT"):
                        events.append({"type":"ENTRY_READY","symbol":symbol,"side":side,"message":f"{symbol} {side}: ENTRY READY","score":score})
                    elif previous and old_score is not None and score is not None and abs(score-old_score) >= 10:
                        direction = "усилен" if score > old_score else "ослаблен"
                        events.append({"type":"SCORE_SHIFT","symbol":symbol,"side":side,"message":f"{symbol} {side}: score {old_score:.0f} → {score:.0f} ({direction})","score":score})
                    if status in ("LONG", "SHORT"):
                        signal = _signal_from_item(x, market, ts)
                        if signal:
                            sid, created = _sqlite_upsert_active(con, signal)
                            if pg:
                                try:
                                    _pg_upsert_active(pg, signal)
                                except Exception as exc:
                                    print(f"POSTGRES ACTIVE SIGNAL ERROR: {type(exc).__name__}: {exc}", flush=True)
                            row = con.execute("SELECT * FROM active_signals WHERE signal_id=?", (sid,)).fetchone()
                            if row:
                                cols = [d[1] for d in con.execute("PRAGMA table_info(active_signals)")]
                                active_items.append(dict(zip(cols, row)))
            con.commit()
            if pg:
                try:
                    with pg.cursor() as cur:
                        for ev in events:
                            cur.execute("INSERT INTO events (ts,symbol,side,event_type,old_status,new_status,new_score,message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (ts,ev.get("symbol"),ev.get("side"),ev["type"],None,None,ev.get("score"),ev["message"]))
                    pg.commit()
                except Exception as exc:
                    print(f"POSTGRES EVENT ERROR: {type(exc).__name__}: {exc}", flush=True)
        finally:
            con.close()
            if pg:
                pg.close()
    _append_active(result, active_items)
    return events


def _update_active_sqlite(con, row, price, ts):
    aid, signal_id, created_ts, updated_ts, market, symbol, side, entry, trigger, sl, tp1, tp2, tp3, rr, score, old_price, regime, h1, m15, m5, rsi, volume_ratio, tda, reason, lifecycle, tp1_hit, tp2_hit, mfe, mae, closed_ts, close_price, close_reason = row
    risk = _risk(entry, sl)
    if price is None or risk in (None, 0):
        return None
    if side == "LONG":
        favorable = (price-entry)/risk
        adverse = (entry-price)/risk
        hit_sl = sl is not None and price <= sl
        hit_tp3 = tp3 is not None and price >= tp3
        hit_tp2 = tp2 is not None and price >= tp2
        hit_tp1 = tp1 is not None and price >= tp1
        r_tp3 = (tp3-entry)/risk if tp3 is not None else None
    else:
        favorable = (entry-price)/risk
        adverse = (price-entry)/risk
        hit_sl = sl is not None and price >= sl
        hit_tp3 = tp3 is not None and price <= tp3
        hit_tp2 = tp2 is not None and price <= tp2
        hit_tp1 = tp1 is not None and price <= tp1
        r_tp3 = (entry-tp3)/risk if tp3 is not None else None
    new_mfe = max(float(mfe or 0), favorable)
    new_mae = max(float(mae or 0), adverse)
    new_tp1 = int(tp1_hit or hit_tp1)
    new_tp2 = int(tp2_hit or hit_tp2)
    close_reason = None
    outcome_r = None
    if hit_sl:
        close_reason, outcome_r = "SL", -1.0
    elif hit_tp3:
        close_reason, outcome_r = "TP3", r_tp3
    new_status = lifecycle
    if close_reason:
        new_status = close_reason
    elif new_tp2:
        new_status = "TP2"
    elif new_tp1:
        new_status = "TP1"
    con.execute("""UPDATE active_signals SET updated_ts=?, price=?, tp1_hit=?, tp2_hit=?, max_favorable_r=?, max_adverse_r=?, lifecycle_status=?, closed_ts=?, close_price=?, close_reason=? WHERE id=?""", (ts,price,new_tp1,new_tp2,new_mfe,new_mae,new_status,ts if close_reason else None,price if close_reason else None,close_reason,aid))
    return {"id":aid,"signal_id":signal_id,"symbol":symbol,"side":side,"status":new_status,"closed":bool(close_reason),"close_reason":close_reason,"price":price,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"mfe":new_mfe,"mae":new_mae}


def update_virtual_outcomes(result):
    """Track persistent active signals by latest observed scan price.

    Scanner status changes do not close a signal. SL or TP3 closes it; TP1/TP2 are
    milestones. Price-only snapshot tracking cannot resolve intrabar ordering.
    """
    ts = now()
    prices = {}
    for key in ("stocks", "futures"):
        for x in result.get(key, []) or []:
            if isinstance(x, dict):
                prices[(str(x.get("symbol", "")).upper(), str(x.get("side", "")).upper())] = _n(x.get("price"))
    closed = []
    with LOCK:
        con = connect()
        pg = _pg_connect()
        try:
            rows = con.execute("SELECT * FROM active_signals WHERE lifecycle_status IN ('ACTIVE','TP1','TP2') ORDER BY id").fetchall()
            for row in rows:
                symbol, side = row[5], row[6]
                price = prices.get((symbol, side))
                change = _update_active_sqlite(con, row, price, ts)
                if change and change["closed"]:
                    closed.append(change)
                if pg:
                    try:
                        with pg.cursor() as cur:
                            cur.execute("SELECT id FROM active_signals WHERE signal_id=%s", (row[1],))
                            pg_row = cur.fetchone()
                            if pg_row:
                                local = con.execute("SELECT tp1_hit,tp2_hit,max_favorable_r,max_adverse_r,lifecycle_status,closed_ts,close_price,close_reason,price,updated_ts FROM active_signals WHERE id=?", (row[0],)).fetchone()
                                cur.execute("""UPDATE active_signals SET updated_ts=%s, price=%s, tp1_hit=%s, tp2_hit=%s, max_favorable_r=%s, max_adverse_r=%s, lifecycle_status=%s, closed_ts=%s, close_price=%s, close_reason=%s WHERE id=%s""", (local[9],local[8],local[0],local[1],local[2],local[3],local[4],local[5],local[6],local[7],pg_row[0]))
                    except Exception as exc:
                        print(f"POSTGRES ACTIVE UPDATE ERROR: {type(exc).__name__}: {exc}", flush=True)
            con.commit()
            if pg:
                pg.commit()
        finally:
            con.close()
            if pg:
                pg.close()
    return closed


def get_active_signals(include_closed=False):
    with LOCK:
        con = connect()
        try:
            if include_closed:
                rows = con.execute("SELECT * FROM active_signals ORDER BY id DESC").fetchall()
            else:
                rows = con.execute("SELECT * FROM active_signals WHERE lifecycle_status IN ('ACTIVE','TP1','TP2') ORDER BY id DESC").fetchall()
            cols = [r[1] for r in con.execute("PRAGMA table_info(active_signals)")]
            return [dict(zip(cols,row)) for row in rows]
        finally:
            con.close()


def close_active_signal(signal_id, price=None, reason="MANUAL_CLOSE"):
    ts = now()
    with LOCK:
        con = connect()
        pg = _pg_connect()
        try:
            con.execute("UPDATE active_signals SET lifecycle_status=?, updated_ts=?, closed_ts=?, close_price=?, close_reason=? WHERE signal_id=? AND lifecycle_status IN ('ACTIVE','TP1','TP2')", (reason,ts,ts,_n(price),reason,signal_id))
            con.commit()
            if pg:
                with pg.cursor() as cur:
                    cur.execute("UPDATE active_signals SET lifecycle_status=%s, updated_ts=%s, closed_ts=%s, close_price=%s, close_reason=%s WHERE signal_id=%s AND lifecycle_status IN ('ACTIVE','TP1','TP2')", (reason,ts,ts,_n(price),reason,signal_id))
                pg.commit()
        finally:
            con.close()
            if pg:
                pg.close()


def daily_stats():
    with LOCK:
        con = connect()
        try:
            total = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            ready = con.execute("SELECT COUNT(*) FROM observations WHERE status IN ('LONG','SHORT')").fetchone()[0]
            closed = con.execute("SELECT COUNT(*) FROM observations WHERE outcome_status != 'OPEN'").fetchone()[0]
            wins = con.execute("SELECT COUNT(*) FROM observations WHERE outcome_status IN ('TP1','TP2','TP3')").fetchone()[0]
            avg_r = con.execute("SELECT AVG(outcome_r) FROM observations WHERE outcome_r IS NOT NULL").fetchone()[0]
            active = con.execute("SELECT COUNT(*) FROM active_signals WHERE lifecycle_status IN ('ACTIVE','TP1','TP2')").fetchone()[0]
            return {"observations":total,"ready":ready,"closed":closed,"wins":wins,"win_rate":(wins/closed*100) if closed else None,"avg_r":avg_r,"active_signals":active}
        finally:
            con.close()
