CREATE SCHEMA IF NOT EXISTS market_data;
CREATE SCHEMA IF NOT EXISTS trading;
CREATE SCHEMA IF NOT EXISTS research;

COMMENT ON SCHEMA market_data IS 'Ingested market data (Binance/Bybit/Polymarket). Tables added in Phase 1.';
COMMENT ON SCHEMA trading    IS 'Orders, fills, positions. Tables added in Phase 2 (Nautilus alignment).';
COMMENT ON SCHEMA research   IS 'Backtests, reports, LLM conversations. Tables added in Phase 2/5.';
