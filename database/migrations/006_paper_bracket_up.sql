-- 006: bracket orders (Paper Trading V2). A resting order can carry its own
-- stop-loss / take-profit, applied to the position when the order fills. This is
-- what "Take this setup" needs: a limit order at the setup entry whose SL/TP
-- activate on fill (and survive a page close). Additive + idempotent; the paper
-- subsystem is isolated from the frozen tables + the determinism stream.
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS sl double precision;
ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS tp double precision;
