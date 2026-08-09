-- ROLLBACK da migration 0013 (F5 — ranking comercial).
--
-- ORDEM OBRIGATORIA: reverter o CODIGO primeiro (git revert do commit que passa
-- limite_produtos/termo_tokens/excluir_ids), este SQL depois. Invertido, o bot
-- passa a chamar parametros que a funcao nao tem mais e TODA busca cai.
--
-- O que volta: a assinatura de 5 args da migration 0009, copiada literalmente
-- (0009 linhas 8-64). NAO recria a sobrecarga de 3 args (dropada pela 0011) —
-- duas candidatas fazem o PostgREST responder "Could not choose the best
-- candidate function" e derrubam a busca inteira.
--
-- O que FICA (aditivo, inofensivo, util fora da F5):
--   - idx_produtos_estoque_id_produto  (acelera consultar_produto_por_id)
--   - public.norm_busca(text)          (funcao pura, sem dependentes)

DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(
  vector, double precision, integer, text, text, text[], text[], integer, integer,
  double precision, numeric);

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

GRANT EXECUTE ON FUNCTION public.buscar_produtos_semantico(
  vector, double precision, integer, text, text) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- Verificacao obrigatoria pos-rollback:
--   select count(*) from pg_proc where proname = 'buscar_produtos_semantico';  -- = 1
