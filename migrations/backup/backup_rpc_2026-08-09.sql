-- Backup das definicoes de buscar_produtos_semantico ANTES da migration 0013 (F5).
-- Gerado por pg_get_functiondef em 2026-08-09. NAO sobrescreve backup_rpc_current.sql (defasado).
-- Sobrecargas existentes no momento do backup: 1

-- === buscar_produtos_semantico(vector,double precision,integer,text,text) ===
CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(query_embedding vector, match_threshold double precision, match_count integer, filtro_tamanho text DEFAULT NULL::text, filtro_id_loja text DEFAULT NULL::text)
 RETURNS TABLE(id_unico text, id_produto text, id_loja text, loja text, nome text, tamanho text, preco numeric, preco_varejo numeric, preco_atacado numeric, estoque numeric, grupo_id text, nome_grupo text, similarity double precision)
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
$function$
;
