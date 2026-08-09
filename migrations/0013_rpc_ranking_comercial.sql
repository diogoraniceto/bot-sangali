-- Migration 0013: ranking comercial dentro da selecao + dedupe por id_produto + excluir_ids
-- Frente 5 do PLANO_CORRECAO_4_DEFEITOS.md (§6).
-- Pre-requisitos: 0001 (normalize_tamanho_tokens), 0002, 0009, 0011 (a sobrecarga
-- de 3 args JA foi dropada pela 0011 — hoje pg_proc tem exatamente 1 candidata).
--
-- REGRA DURA: nenhuma migration posterior pode recriar a assinatura de 5 args de
-- public.buscar_produtos_semantico. Com duas candidatas o PostgREST responde
-- "Could not choose the best candidate function" a TODA chamada nomeada do bot e
-- a busca cai inteira, sem erro de sintaxe e sem deploy novo.
--
-- ORDEM DE DEPLOY: esta migration PRIMEIRO, o codigo depois. Ela e compativel
-- para tras — os 6 parametros novos tem DEFAULT, entao a chamada de 5 args que o
-- bot faz hoje continua resolvendo. (O rollback e o inverso: codigo primeiro.)

-- ---------------------------------------------------------------------------
-- 1. Indice em id_produto
-- ---------------------------------------------------------------------------
-- Aditivo e util alem da F5: hoje a query de consultar_produto_por_id
-- (.eq("id_produto", ...)) e Seq Scan descartando ~6.730 linhas.
-- NAO e usado pela RPC abaixo (ela roda com enable_indexscan = off) — fica de
-- proposito, e o rollback 0013 NAO o remove.
CREATE INDEX IF NOT EXISTS idx_produtos_estoque_id_produto
  ON public.produtos_estoque (id_produto);

-- ---------------------------------------------------------------------------
-- 2. Normalizador lexical (mesmo translate de normalize_tamanho_tokens)
-- ---------------------------------------------------------------------------
-- Serve ao escopo de CATEGORIA da tier 1, que e LEXICAL e nao semantico.
-- Unifica 'SUTIÃ' (77 produtos) com 'SUTIA' (43) — sem isto o head-noun de um
-- nunca casaria com o do outro.
CREATE OR REPLACE FUNCTION public.norm_busca(txt text)
RETURNS text LANGUAGE sql IMMUTABLE AS $f$
  SELECT upper(translate(coalesce(txt,''),
    'ÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑáàâãäåéèêëíìîïóòôõöúùûüçñ',
    'AAAAAAEEEEIIIIOOOOOUUUUCNAAAAAAEEEEIIIIOOOOOUUUUCN'));
$f$;

-- ---------------------------------------------------------------------------
-- 3. A RPC
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(
  vector, double precision, integer, text, text);

CREATE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector,
    match_threshold double precision,
    match_count integer,
    filtro_tamanho text DEFAULT NULL,
    filtro_id_loja text DEFAULT NULL,
    excluir_ids text[] DEFAULT NULL,
    termo_tokens text[] DEFAULT NULL,
    limite_produtos integer DEFAULT 8,
    ancora_semantica integer DEFAULT 2,
    janela_similaridade double precision DEFAULT 0.05,
    minimo_grade numeric DEFAULT 3
)
RETURNS TABLE(
    id_unico text, id_produto text, id_loja text, loja text, nome text, tamanho text,
    preco numeric, preco_varejo numeric, preco_atacado numeric, estoque numeric,
    grupo_id text, nome_grupo text, similarity double precision,
    estoque_grade numeric, n_tamanhos integer, destaque boolean, tier smallint
)
LANGUAGE sql
STABLE
-- KNN EXATO de proposito: com filtro (loja/tamanho/estoque) o HNSW devolve MENOS
-- linhas que o pool pedido (medido: 13-48 de 60), e um pool curto e exatamente o
-- que a F5 existe para consertar. Exato devolve o pool cheio sempre, em ~10-20 ms
-- (a tabela tem ~6.700 linhas). Se passar de ~50k linhas, trocar os dois SET por
-- SET hnsw.iterative_scan = 'relaxed_order'.
SET enable_indexscan = off
SET enable_bitmapscan = off
AS $function$
WITH
-- Guards de array. 'x <> ALL(NULL)' e 'x = ANY(array[...,NULL])' devolvem NULL e
-- DESCARTAM a linha em silencio — seria toda busca sem exclusoes voltando vazia.
-- array_remove mata o caso do NULL dentro do array de uma vez.
args AS (
  SELECT public.normalize_tamanho_tokens(filtro_tamanho) AS toks,
         array_remove(coalesce(excluir_ids, '{}'::text[]), NULL) AS excl,
         array_remove(coalesce(termo_tokens, '{}'::text[]), NULL) AS termos
),
cand AS (
  SELECT p.id_unico, p.id_produto, p.id_loja, p.loja, p.nome, p.tamanho, p.preco,
         p.preco_varejo, p.preco_atacado, p.estoque, p.grupo_id, p.nome_grupo,
         1 - (p.embedding <=> query_embedding) AS sim
  FROM produtos_estoque p, args
  WHERE p.estoque > 0
    AND p.embedding IS NOT NULL
    AND (filtro_tamanho IS NULL OR cardinality(args.toks) = 0
         OR p.tamanho_tokens && args.toks)
    AND (filtro_id_loja IS NULL OR p.id_loja::text = filtro_id_loja)
    AND (cardinality(args.excl) = 0 OR NOT (p.id_produto = ANY (args.excl)))
    AND 1 - (p.embedding <=> query_embedding) > match_threshold
  ORDER BY p.embedding <=> query_embedding
  LIMIT GREATEST(match_count, limite_produtos)
),
cg AS (
  SELECT c.*,
         SUM(c.estoque) OVER (PARTITION BY c.id_produto) AS grade_pool,
         COUNT(*)       OVER (PARTITION BY c.id_produto) AS n_tam
  FROM cand c
),
-- 1 linha REPRESENTATIVA por id_produto (a de maior estoque). O contrato da tool
-- continua "uma linha = um produto+tamanho" de proposito: array de tamanhos
-- quebraria extrair_produtos_de_tool_results e o cache de cards (backlog B1).
-- preco e UNICO por (id_produto,id_loja), entao a escolha nao muda o valor citado.
rep AS (
  SELECT DISTINCT ON (id_produto) * FROM cg
  ORDER BY id_produto, estoque DESC, sim DESC, id_unico
),
-- Grade VERDADEIRA dos bestsellers do pool. Uma varredura agregada, nao N
-- subqueries correlacionadas: com enable_indexscan = off cada subquery escalar
-- seria um Seq Scan proprio (ate 60 x 6.700 linhas).
best AS (
  SELECT pe.id_produto, sum(pe.estoque) AS grade_total
  FROM produtos_estoque pe
  WHERE pe.estoque > 0
    AND (filtro_id_loja IS NULL OR pe.id_loja::text = filtro_id_loja)
    AND pe.id_produto IN (SELECT r.id_produto FROM rep r
                          WHERE coalesce(r.nome_grupo,'') = 'PRODUTOS MAIS VENDIDOS')
  GROUP BY 1
),
rk AS (
  SELECT r.*,
         MAX(r.sim) OVER () AS sim_max,
         ROW_NUMBER() OVER (ORDER BY r.sim DESC, r.id_produto) AS rn_sim,
         coalesce(b.grade_total, r.grade_pool) AS grade_real
  FROM rep r LEFT JOIN best b ON b.id_produto = r.id_produto
),
top1 AS (
  SELECT split_part(public.norm_busca(nome), ' ', 1) AS head FROM rk WHERE rn_sim = 1
),
cl AS (
  SELECT rk.*,
    CASE
      -- tier 0: ancora semantica. A categoria pedida NUNCA perde o topo.
      WHEN rk.rn_sim <= ancora_semantica THEN 0::smallint
      -- tier 1: bestseller do ERP, DENTRO da janela, com grade minima
      --         E DENTRO DA CATEGORIA.
      -- O escopo de categoria e LEXICAL porque a janela de cosseno NAO escopa:
      -- com filtro_tamanho o sim_max desaba e 0,05 engole o pool inteiro (medido:
      -- fantasia+G devolvia 6 de 8 em tier 1, todos CALCINHA/PIJAMA).
      -- E e pelo NOME, nao pelo grupo, porque os grupos do GestaoClick sao
      -- MUTUAMENTE EXCLUSIVOS: quem entra em 'PRODUTOS MAIS VENDIDOS' perde o
      -- grupo de categoria. Heuristico por limitacao de dado (§10.3 item 10).
      WHEN coalesce(rk.nome_grupo,'') = 'PRODUTOS MAIS VENDIDOS'
           AND rk.sim >= rk.sim_max - janela_similaridade
           AND rk.grade_real >= minimo_grade
           AND ( EXISTS (SELECT 1 FROM args, unnest(args.termos) t
                         WHERE public.norm_busca(rk.nome)
                               LIKE '%' || public.norm_busca(t) || '%')
              OR split_part(public.norm_busca(rk.nome), ' ', 1) = (SELECT head FROM top1) )
        THEN 1::smallint
      ELSE 2::smallint
    END AS tier
  FROM rk
)
SELECT id_unico, id_produto, id_loja, loja, nome, tamanho, preco, preco_varejo,
       preco_atacado, estoque, grupo_id, nome_grupo,
       sim AS similarity,
       grade_real AS estoque_grade,
       n_tam::integer AS n_tamanhos,
       -- selo = PROMOVIDO NA CATEGORIA, nao "pertence ao grupo do ERP".
       -- Com o selo colado no grupo, CONJUNTO MOANA saia com destaque=true numa
       -- busca de fantasia — e a linha 51 do prompt PROIBE chamar CONJUNTO de fantasia.
       (tier = 1) AS destaque,
       tier
FROM cl
-- Dentro da tier 1 a profundidade de grade desempata entre campeoes; ela NUNCA
-- promove nada de fora do grupo curado (estoque alto fora da curadoria pode ser
-- encalhe — nao ha dado de venda por item, `vendas` e so cabecalho).
ORDER BY tier ASC,
         (CASE WHEN tier = 1 THEN grade_real ELSE 0 END) DESC,
         sim DESC,
         id_produto
LIMIT limite_produtos;
$function$;

GRANT EXECUTE ON FUNCTION public.buscar_produtos_semantico(
  vector, double precision, integer, text, text, text[], text[], integer, integer,
  double precision, numeric) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.norm_busca(text) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
