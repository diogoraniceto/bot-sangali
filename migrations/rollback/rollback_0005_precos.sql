-- ROLLBACK 0005: remove colunas preco_varejo_avista e preco_atacado_aprazo
-- ATENÇÃO: aplicar este script ANTES de fazer rollback do código que lê essas colunas.
ALTER TABLE produtos_estoque
  DROP COLUMN IF EXISTS preco_varejo_avista,
  DROP COLUMN IF EXISTS preco_atacado_aprazo;
