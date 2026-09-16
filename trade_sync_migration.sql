ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS source_trade_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_source_trade_id
ON public.trades(source_trade_id)
WHERE source_trade_id IS NOT NULL;

ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS active_signal_id BIGINT;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp1_hit_ts TIMESTAMPTZ;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp2_hit_ts TIMESTAMPTZ;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS tp3_hit_ts TIMESTAMPTZ;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS signal_status TEXT;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS max_favorable_r DOUBLE PRECISION DEFAULT 0;
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS max_adverse_r DOUBLE PRECISION DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_trades_active_signal_id
ON public.trades(active_signal_id);
