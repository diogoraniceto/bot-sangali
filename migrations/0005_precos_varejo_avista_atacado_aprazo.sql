-- Migration 0005: separar todos os 4 tipos de preço retornados pela API GestãoClick
-- Causa-raiz: sync_erp.py procurava nome_tipo == 'Varejo'/'Atacado' (literal),
-- mas a API retorna 'VAREJO CRÉDITO', 'VAREJO A VISTA', 'ATACADO A VISTA',
-- 'ATACADO CRÉDITO'. Match nunca casava, fallback fazia atacado = varejo.
--
-- Mapeamento final:
--   VAREJO CRÉDITO   -> preco_varejo          (preço de tabela mostrado)
--   VAREJO A VISTA   -> preco_varejo_avista   (NOVO — uso futuro, não consumido pelo bot ainda)
--   ATACADO A VISTA  -> preco_atacado         (atacado à vista, melhor preço)
--   ATACADO CRÉDITO  -> preco_atacado_aprazo  (NOVO — atacado parcelado real do ERP)

ALTER TABLE produtos_estoque
  ADD COLUMN IF NOT EXISTS preco_varejo_avista numeric,
  ADD COLUMN IF NOT EXISTS preco_atacado_aprazo numeric;
