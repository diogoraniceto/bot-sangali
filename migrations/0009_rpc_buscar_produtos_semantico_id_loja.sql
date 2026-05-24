-- Migration 0009: adiciona parametro filtro_id_loja em buscar_produtos_semantico
-- Pré-requisito: 0002 aplicada (assinatura 4-args atual).
-- A nova assinatura tem 5 args. PostgreSQL trata como overload — para evitar
-- ambiguidade com chamadas por nome, dropamos a versão de 4 args antes.

DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(vector, double precision, integer, text);

CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector,
    match_threshold double precision,
    match_count integer,
    filtro_tamanho text DEFAULT NULL,
    filtro_id_loja text DEFAULT NULL
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
    AND (
      filtro_id_loja IS NULL
      OR p.id_loja::text = filtro_id_loja
    )
    AND 1 - (p.embedding <=> query_embedding) > match_threshold
  ORDER BY p.embedding <=> query_embedding
  LIMIT match_count;
$function$;
