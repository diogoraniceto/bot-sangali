-- Migration 0004: tabela promocoes_ativas (Dia S e similares)
-- Resolve Risco 3 da auditoria (Dia S inventado).
-- O bot consulta esta tabela em vez de tentar adivinhar dia/categoria.

CREATE TABLE IF NOT EXISTS promocoes_ativas (
  id                serial      PRIMARY KEY,
  nome              text        NOT NULL,
  dia_semana        int         CHECK (dia_semana BETWEEN 0 AND 6),  -- 0=domingo, 1=segunda, ...
  categoria         text,
  percentual        int         CHECK (percentual BETWEEN 0 AND 100),
  formas_pagamento  text[]      DEFAULT ARRAY[]::text[],
  troca_permitida   boolean     DEFAULT false,
  ativa             boolean     DEFAULT true,
  observacao        text,
  created_at        timestamptz DEFAULT now(),
  updated_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_promocoes_ativas_dia
  ON promocoes_ativas (dia_semana) WHERE ativa = true;

-- Seed inicial: Dia S configurado conforme prompt_luna_24.04.txt seção 11.
-- IMPORTANTE: a categoria do Dia S muda semanalmente. Esta linha é
-- apenas um placeholder. Atualize 'categoria' a cada início de semana
-- via UPDATE direto ou painel administrativo.
INSERT INTO promocoes_ativas (nome, dia_semana, categoria, percentual, formas_pagamento, troca_permitida, observacao)
SELECT 'Dia S', 1, 'a definir semanalmente', 20, ARRAY['pix','dinheiro'], false,
       'Categoria varia toda semana. Atualizar via UPDATE antes de cada segunda-feira.'
WHERE NOT EXISTS (SELECT 1 FROM promocoes_ativas WHERE nome = 'Dia S');
