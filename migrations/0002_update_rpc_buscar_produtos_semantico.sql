-- Migration 0002: atualiza buscar_produtos_semantico para usar tokens em vez de match estrito
-- Pré-requisito: 0001 aplicada (coluna tamanho_tokens populada).
-- Resolve Risco 4 da auditoria.
-- IMPORTANTE: usa CREATE OR REPLACE com a MESMA assinatura/return type
-- da função existente (4 args, todos os ids text). Não altera overload de 3 args.

CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector,
    match_threshold double precision,
    match_count integer,
    filtro_tamanho text DEFAULT NULL
)
RETURNS TABLE(
    id_unico text,
    id_produto text,
    id_loja text,
    loja text,
    nome text,
    tamanho text,
    preco numeric,
    preco_varejo numeric,
    preco_atacado numeric,
    estoque numeric,
    grupo_id text,
    nome_grupo text,
    similarity double precision
)
LANGUAGE sql
STABLE
AS $function$
  WITH tokens_alvo AS (
    SELECT public.normalize_tamanho_tokens(filtro_tamanho) AS toks
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
$function$;
