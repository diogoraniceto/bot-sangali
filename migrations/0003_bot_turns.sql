-- Migration 0003: tabela bot_turns para logging estruturado por turno
-- Resolve a Dimensão 7 da auditoria (observabilidade).
-- Permite reconstruir exatamente o que aconteceu em qualquer interação:
-- input do usuário, tool calls executadas, args, resultados, output final.

CREATE TABLE IF NOT EXISTS bot_turns (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     text        NOT NULL,
  turn_idx    int,
  created_at  timestamptz NOT NULL DEFAULT now(),
  user_input  text,
  tool_calls  jsonb,
  final_output text,
  output_format text,
  fallback_used boolean DEFAULT false,
  latency_ms  int,
  model       text,
  error       text
);

CREATE INDEX IF NOT EXISTS idx_bot_turns_user_created
  ON bot_turns (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bot_turns_created
  ON bot_turns (created_at DESC);

-- Função de retenção (rodar via pg_cron ou job externo)
-- Mantém apenas últimos 30 dias para controlar egress.
CREATE OR REPLACE FUNCTION purge_old_bot_turns()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
  rows_deleted bigint;
BEGIN
  DELETE FROM bot_turns
  WHERE created_at < now() - INTERVAL '30 days';
  GET DIAGNOSTICS rows_deleted = ROW_COUNT;
  RETURN rows_deleted;
END;
$$;

-- Para agendar (opcional, requer pg_cron habilitado):
--   SELECT cron.schedule('purge_bot_turns', '0 3 * * *', 'SELECT purge_old_bot_turns()');
