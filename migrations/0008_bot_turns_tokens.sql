-- Migration 0008: tokens Gemini em bot_turns para tracking de custo
-- Idempotente.

ALTER TABLE bot_turns
  ADD COLUMN IF NOT EXISTS tokens_in int,
  ADD COLUMN IF NOT EXISTS tokens_out int,
  ADD COLUMN IF NOT EXISTS tokens_total int;
