import os
import sqlite3
import threading

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
SQLITE_PATH = os.getenv('AA_JOURNAL_DB', '/tmp/aa_analitik_journal.sqlite3')
SYNC_INTERVAL = float(os.getenv('TRADE_SYNC_INTERVAL', '5'))
STOP = threading.Event()


def _pg_connect():
    if not DATABASE_URL:
        return None
    try:
        import psycopg
        return psycopg.connect(DATABASE_URL, connect_timeout=8)
    except Exception as exc:
        print(f'TRADE SYNC POSTGRES CONNECT ERROR: {type(exc).__name__}: {exc}', flush=True)
        return None


def _ensure_schema(pg):
    with pg.cursor() as cur:
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS source_trade_id TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_source_trade_id ON public.trades(source_trade_id) WHERE source_trade_id IS NOT NULL")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS active_signal_id BIGINT")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp1_hit_ts TIMESTAMPTZ")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp2_hit_ts TIMESTAMPTZ")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp3_hit_ts TIMESTAMPTZ")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS signal_status TEXT")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS max_favorable_r DOUBLE PRECISION DEFAULT 0")
        cur.execute("ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS max_adverse_r DOUBLE PRECISION DEFAULT 0")
    pg.commit()


def _sqlite_rows():
    if not os.path.exists(SQLITE_PATH):
        return []
    con = sqlite3.connect(SQLITE_PATH, timeout=10)
    try:
        con.execute('PRAGMA busy_timeout=5000')
        columns = [r[1] for r in con.execute('PRAGMA table_info(trades)')]
        if not columns:
            return []
        return [dict(zip(columns, row)) for row in con.execute('SELECT * FROM trades ORDER BY id ASC').fetchall()]
    except Exception as exc:
        print(f'TRADE SYNC SQLITE READ ERROR: {type(exc).__name__}: {exc}', flush=True)
        return []
    finally:
        con.close()


def _upsert_row(pg, row):
    source_id = str(row.get('id')) if row.get('id') is not None else None
    if not source_id:
        return
    columns = [
        'created_ts','chat_id','market','symbol','side','entry','sl','tp1','tp2','tp3',
        'quantity','risk_rub','rr','strategy','timeframe','scanner_score','scanner_trigger',
        'scanner_price','scanner_rr','scanner_regime','scanner_h1','scanner_m15','scanner_m5',
        'scanner_rsi','scanner_volume_ratio','scanner_trigger_distance_atr','scanner_reason',
        'notes','status','close_price','result_r','closed_ts','close_side','parent_trade_id',
        'close_notes','active_signal_id','tp1_hit_ts','tp2_hit_ts','tp3_hit_ts','signal_status',
        'max_favorable_r','max_adverse_r','source_trade_id'
    ]
    values = [row.get(c) for c in columns]
    values[-1] = source_id
    with pg.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='trades'")
        existing = {r[0] for r in cur.fetchall()}
        usable = [c for c in columns if c in existing]
        if 'source_trade_id' not in usable:
            return
        vals = [values[columns.index(c)] for c in usable]
        placeholders = ','.join(['%s'] * len(usable))
        assignments = ','.join(f'{c}=EXCLUDED.{c}' for c in usable if c != 'source_trade_id')
        cur.execute(
            f"INSERT INTO public.trades ({','.join(usable)}) VALUES ({placeholders}) "
            f"ON CONFLICT (source_trade_id) DO UPDATE SET {assignments}",
            vals,
        )


def sync_once():
    rows = _sqlite_rows()
    if not rows:
        return 0
    pg = _pg_connect()
    if pg is None:
        return 0
    try:
        _ensure_schema(pg)
        count = 0
        for row in rows:
            try:
                _upsert_row(pg, row)
                count += 1
            except Exception as exc:
                print(f"TRADE SYNC ROW ERROR id={row.get('id')}: {type(exc).__name__}: {exc}", flush=True)
        pg.commit()
        return count
    except Exception as exc:
        print(f'TRADE SYNC ERROR: {type(exc).__name__}: {exc}', flush=True)
        try:
            pg.rollback()
        except Exception:
            pass
        return 0
    finally:
        pg.close()


def _worker():
    print('TRADE SYNC WORKER STARTED', flush=True)
    while not STOP.is_set():
        try:
            count = sync_once()
            if count:
                print(f'TRADE SYNCED: {count}', flush=True)
        except Exception as exc:
            print(f'TRADE SYNC WORKER ERROR: {type(exc).__name__}: {exc}', flush=True)
        STOP.wait(SYNC_INTERVAL)
    print('TRADE SYNC WORKER STOPPED', flush=True)


def start():
    if not DATABASE_URL:
        print('TRADE SYNC DISABLED: DATABASE_URL is not set', flush=True)
        return None
    thread = threading.Thread(target=_worker, daemon=True, name='trade-sync-worker')
    thread.start()
    return thread


def stop():
    STOP.set()


def list_supabase_trades(limit=20):
    if not DATABASE_URL:
        return []
    pg = _pg_connect()
    if pg is None:
        return []
    try:
        with pg.cursor() as cur:
            cur.execute('SELECT id,created_ts,symbol,side,entry,sl,tp1,tp2,tp3,quantity,risk_rub,rr,status,close_price,result_r,closed_ts,source_trade_id,signal_status FROM public.trades ORDER BY id DESC LIMIT %s', (max(1, min(int(limit), 100)),))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        print(f'TRADE SUPABASE READ ERROR: {type(exc).__name__}: {exc}', flush=True)
        return []
    finally:
        pg.close()


def list_active_signals(limit=20):
    if not DATABASE_URL:
        return []
    pg = _pg_connect()
    if pg is None:
        return []
    try:
        with pg.cursor() as cur:
            cur.execute('SELECT id,signal_id,created_ts,updated_ts,market,symbol,side,entry,trigger,sl,tp1,tp2,tp3,rr,score,price,regime,h1,m15,m5,rsi,volume_ratio,trigger_distance_atr,reason,lifecycle_status,tp1_hit,tp2_hit,max_favorable_r,max_adverse_r,closed_ts,close_price,close_reason FROM public.active_signals ORDER BY id DESC LIMIT %s', (max(1, min(int(limit), 100)),))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        print(f'ACTIVE SIGNAL READ ERROR: {type(exc).__name__}: {exc}', flush=True)
        return []
    finally:
        pg.close()
