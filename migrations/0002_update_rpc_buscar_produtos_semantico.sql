-- Migration 0002: atualiza buscar_produtos_semantico para usar tokens em vez de match estrito
-- Pré-requisito: 0001 aplicada (coluna tamanho_tokens populada).
-- Resolve Risco 4 da auditoria.

CREATE OR REPLACE FUNCTION buscar_produtos_semantico(
    query_embedding vector(768),
    match_threshold float,
    match_count int,
    filtro_tamanho text
)
RETURNS TABLE (
    id_unico text,
    id_produto bigint,
    id_loja bigint,
    loja text,
    nome text,
    tamanho text,
    preco numeric,
    preco_varejo numeric,
    preco_atacado numeric,
    estoque numeric,
    grupo_id bigint,
    nome_grupo text,
    similarity float
)
LANGUAGE sql
STABLE
AS $$
  WITH tokens_alvo AS (
    SELECT normalize_tamanho_tokens(filtro_tamanho) AS toks
  )
  SELECT
      p.id_unico,
      p.id_produto,
      p.id_loja,
      p.loja,
      p.nome,
      p.tamanho,
      p.preco,
      p.preco_varejo,
      p.preco_atacado,
      p.estoque,
      p.grupo_id,
      p.nome_grupo,
      1 - (p.embedding <=> query_embedding) AS similarity
  FROM produtos_estoque p, tokens_alvo
  WHERE p.estoque > 0
    AND (
      filtro_tamanho IS NULL
      OR cardinality(tokens_alvo.toks) = 0
      OR p.tamanho_tokens && tokens_alvo.toks
    )
    AND 1 - (p.embedding <=> query_embedding) > match_threshold
  ORDER BY p.embedding <=> query_embedding
  LIMIT match_count;
$$;
