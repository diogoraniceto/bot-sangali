-- Migration 0007: calendário de Dia S por semana
-- Permite a equipe pré-cadastrar a categoria do Dia S de várias semanas.
-- Bot consulta a view vw_promocao_ativa_hoje que junta o que está agendado
-- com promocoes_ativas (regras semanais persistentes).
-- Idempotente.

CREATE TABLE IF NOT EXISTS dia_s_calendario (
  id           serial      PRIMARY KEY,
  data_inicio  date        NOT NULL,
  data_fim     date        NOT NULL,
  categoria    text        NOT NULL,
  percentual   int         CHECK (percentual BETWEEN 0 AND 100),
  formas_pagamento text[]  DEFAULT ARRAY['pix','dinheiro']::text[],
  observacao   text,
  created_at   timestamptz DEFAULT now(),
  CHECK (data_fim >= data_inicio)
);

CREATE INDEX IF NOT EXISTS idx_dia_s_periodo
  ON dia_s_calendario (data_inicio, data_fim);

-- View que retorna a promoção válida para hoje (timezone America/Sao_Paulo).
-- Prioriza dia_s_calendario (agendamento específico) sobre promocoes_ativas
-- (regra semanal persistente).
CREATE OR REPLACE VIEW vw_promocao_ativa_hoje AS
WITH agora AS (
  SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date AS hoje,
         EXTRACT(DOW FROM (now() AT TIME ZONE 'America/Sao_Paulo'))::int AS dow
),
agendado AS (
  SELECT 'calendario'::text AS fonte,
         d.categoria,
         d.percentual,
         d.formas_pagamento,
         d.observacao,
         (SELECT dow FROM agora) AS dia_semana
  FROM dia_s_calendario d, agora a
  WHERE a.hoje BETWEEN d.data_inicio AND d.data_fim
  ORDER BY d.data_inicio DESC
  LIMIT 1
),
semanal AS (
  SELECT 'semanal'::text AS fonte,
         p.categoria,
         p.percentual,
         p.formas_pagamento,
         p.observacao,
         p.dia_semana
  FROM promocoes_ativas p, agora a
  WHERE p.ativa = true
    AND p.dia_semana = a.dow
)
SELECT * FROM agendado
UNION ALL
SELECT * FROM semanal
WHERE NOT EXISTS (SELECT 1 FROM agendado);
