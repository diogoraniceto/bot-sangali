-- BACKUP: definição atual da função buscar_produtos_semantico
-- Capturado de produção em 2026-05-06 antes de aplicar migration 0002.
-- Use este arquivo se precisar reverter a RPC para o estado anterior.

-- Overload 3 args (legacy, não usado pelo bot mas preservado)
CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector,
    match_threshold double precision,
    match_count integer
)
RETURNS TABLE(
    id_produto text,
    nome text,
    tamanho text,
    preco numeric,
    similarity double precision
)
LANGUAGE sql
STABLE
AS $function$
  select
    id_produto,
    nome,
    tamanho,
    preco,
    1 - (produtos_estoque.embedding <=> query_embedding) as similarity
  from produtos_estoque
  where 1 - (produtos_estoque.embedding <=> query_embedding) > match_threshold
  order by similarity desc
  limit match_count;
$function$;

-- Overload 4 args (atual, usado pelo bot)
CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector,
    match_threshold double precision,
    match_count integer,
    filtro_tamanho text DEFAULT NULL::text
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
    FROM produtos_estoque p
    WHERE p.estoque > 0
      AND (filtro_tamanho IS NULL OR upper(p.tamanho) = upper(filtro_tamanho))
      AND 1 - (p.embedding <=> query_embedding) > match_threshold
    ORDER BY p.embedding <=> query_embedding
    LIMIT match_count;
$function$;
