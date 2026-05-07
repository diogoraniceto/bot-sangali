-- Migration 0001: normalizar tamanho via tokens
-- Resolve Risco 4 (tamanho falso vazio por formatação divergente)
-- Idempotente: pode rodar múltiplas vezes sem efeito colateral.

-- Função que converte um valor cru de tamanho em array de tokens canônicos
-- "42 - G"   -> {"42","G"}
-- "G1"       -> {"G1"}
-- "ÚNICO"    -> {"UNICO"}
-- "42(G1)"   -> {"42","G1"}
-- "P / M"    -> {"P","M"}
CREATE OR REPLACE FUNCTION normalize_tamanho_tokens(t text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT COALESCE(
    array(
      SELECT trim(token)
      FROM unnest(
        regexp_split_to_array(
          upper(translate(
            COALESCE(t, ''),
            'ÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑáàâãäåéèêëíìîïóòôõöúùûüçñ',
            'AAAAAAEEEEIIIIOOOOOUUUUCNAAAAAAEEEEIIIIOOOOOUUUUCN'
          )),
          '[\s\-\/\(\)\,\|]+'
        )
      ) AS token
      WHERE trim(token) <> ''
    ),
    ARRAY[]::text[]
  );
$$;

-- Coluna tamanho_tokens em produtos_estoque
ALTER TABLE produtos_estoque
  ADD COLUMN IF NOT EXISTS tamanho_tokens text[];

-- Backfill
UPDATE produtos_estoque
SET tamanho_tokens = normalize_tamanho_tokens(tamanho)
WHERE tamanho_tokens IS NULL OR tamanho_tokens = ARRAY[]::text[];

-- Trigger para manter tamanho_tokens em sincronia com tamanho
CREATE OR REPLACE FUNCTION trg_set_tamanho_tokens()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.tamanho_tokens := normalize_tamanho_tokens(NEW.tamanho);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS produtos_estoque_set_tamanho_tokens ON produtos_estoque;
CREATE TRIGGER produtos_estoque_set_tamanho_tokens
  BEFORE INSERT OR UPDATE OF tamanho ON produtos_estoque
  FOR EACH ROW
  EXECUTE FUNCTION trg_set_tamanho_tokens();

-- Índice GIN para match_array eficiente
CREATE INDEX IF NOT EXISTS idx_produtos_estoque_tamanho_tokens
  ON produtos_estoque USING GIN (tamanho_tokens);
