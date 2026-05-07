-- ROLLBACK 0002: restaura RPC buscar_produtos_semantico (4 args) ao estado anterior
-- Para rollback completo: aplicar este antes do rollback 0001.

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
