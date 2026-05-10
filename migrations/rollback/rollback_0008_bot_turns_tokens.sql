ALTER TABLE bot_turns
  DROP COLUMN IF EXISTS tokens_in,
  DROP COLUMN IF EXISTS tokens_out,
  DROP COLUMN IF EXISTS tokens_total;
