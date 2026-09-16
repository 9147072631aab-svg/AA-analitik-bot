AA Analitik — Supabase trade persistence v3.2.6

Files:
- app.py — complete replacement for current app.py
- trade_sync.py — new module; mirrors bot SQLite trades into Supabase and exposes readers
- trade_sync_migration.sql — idempotent Supabase migration

What this step adds:
1. SQLite trade records are continuously mirrored into public.trades in Supabase.
2. Existing trade IDs are preserved as source_trade_id, so repeated syncs do not duplicate records.
3. TP1/TP2/TP3 milestone fields and signal lifecycle fields are supported.
4. /active-signals reads persistent active signal lifecycles from Supabase.
5. /trades reads persistent trade records from Supabase.
6. Existing bot algorithm and broker behavior are not changed; the bot still never sends broker orders.

Important:
The current bot.py still uses its existing SQLite journal for its Telegram UI. This step adds durable Supabase synchronization without rewriting the large bot.py file. The next migration can switch the Telegram UI itself to read/write Supabase directly.

Validation:
app.py and trade_sync.py pass Python syntax compilation.
