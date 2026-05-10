-- Produtos onde preco_atacado >= preco_varejo (cadastro invertido no ERP).
-- Esses casos não têm desconto real de atacado e precisam ser revisados
-- manualmente no GestãoClick.
-- O bot já neutraliza em runtime (calcular_total aplica desconto configurável
-- quando detecta inversão), mas o cadastro correto melhora a margem real.

SELECT
    id_produto,
    nome,
    tamanho,
    preco_varejo,
    preco_atacado,
    preco_atacado - preco_varejo AS dif_invertida,
    last_sync
FROM produtos_estoque
WHERE estoque > 0
  AND preco_atacado >= preco_varejo
ORDER BY (preco_atacado - preco_varejo) DESC NULLS LAST,
         id_produto;
