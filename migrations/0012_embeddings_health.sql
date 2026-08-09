-- Migration 0012: funcao de saude dos embeddings (metrica do /health + alarme)
-- Frente 4. Numeracao reservada no PLANO_CORRECAO_4_DEFEITOS.md (§1):
--   F2 = 0010 e 0011 · F4 = 0012 · F5 = 0013.
--
-- Por que RPC e nao coluna gerada / view materializada:
--   uma coluna obrigaria rewrite de produtos_estoque, e a metrica precisa ser
--   lida pelo processo WEB (o /health), que pode nao ser quem roda o sync.
-- Por que grant a anon: a chave que o bot usa e a anon (JWT role=anon), igual a
--   todas as outras tabelas/funcoes do projeto.
--
-- Chamada pelo bot: supabase.rpc("embeddings_health").execute()
-- O consumidor no /health tem cache de 10 min: e sonda de liveness do Railway e
-- nao pode virar 1 query por probe.

CREATE OR REPLACE FUNCTION public.embeddings_health()
RETURNS TABLE (
    linhas_total bigint,
    nulos_total bigint,
    nulos_com_estoque bigint,
    produtos_com_nulo bigint,
    nulos_mais_vendidos bigint
)
LANGUAGE sql
STABLE
AS $function$
  SELECT
      count(*),
      count(*) FILTER (WHERE embedding IS NULL),
      count(*) FILTER (WHERE embedding IS NULL AND estoque > 0),
      count(DISTINCT id_produto) FILTER (WHERE embedding IS NULL),
      count(*) FILTER (WHERE embedding IS NULL AND estoque > 0
                       AND upper(coalesce(nome_grupo, '')) = 'PRODUTOS MAIS VENDIDOS')
  FROM public.produtos_estoque;
$function$;

GRANT EXECUTE ON FUNCTION public.embeddings_health() TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
