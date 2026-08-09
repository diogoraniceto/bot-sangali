-- Migration 0011: dropa a sobrecarga LEGADA de 3 args de buscar_produtos_semantico.
-- Frente 2. Numeracao reservada no PLANO_CORRECAO_4_DEFEITOS.md (§1).
--
-- Por que dropar: com DUAS candidatas (a legada de 3 args, sem DEFAULT, e a viva de
-- 5 args, com DEFAULT nos dois ultimos), toda chamada com 3 argumentos e AMBIGUA.
-- Nao e teoria — medido nas duas vias, antes deste drop:
--
--   SQL direto:  ERROR 42725: function buscar_produtos_semantico(vector, numeric,
--                integer) is not unique / HINT: Could not choose a best candidate
--   PostgREST:   "Could not choose the best candidate function between: ..."
--
-- Consequencia: a legada e INALCANCAVEL sem erro. Nenhum consumidor pode estar
-- dependendo dela com sucesso hoje — e por isso o drop nao pode quebrar caller
-- nenhum; ao contrario, um caller de 3 chaves passa a FUNCIONAR (resolve na de 5,
-- com filtro_tamanho/filtro_id_loja em NULL). Isso fecha a precondicao que o plano
-- deixou aberta (`pg_stat_statements` desta instancia nao tem `stats_since`, logo as
-- 16 chamadas registradas sao indataveis e nao provam nada).
--
-- Evidencia adicional coletada antes de aplicar:
--   * edge functions do projeto: NENHUMA (GET /v1/projects/.../functions -> []).
--   * pg_depend: 0 dependentes de oid 252201 (fora do proprio i).
--   * unico caller no repo: bot.py (supabase.rpc), sempre com as 5 chaves nomeadas.
--
-- A legada tambem era pior por construcao: RETURNS TABLE de 5 colunas, sem filtro de
-- loja e sem filtro de estoque — resolver na assinatura errada devolvia produto de
-- outra filial e produto com estoque zero.
--
-- REGRA DURA (migrations/README.md): depois disto, `select count(*) from pg_proc
-- where proname='buscar_produtos_semantico'` tem de ser 1. Recriar uma segunda
-- candidata derruba TODA a busca do bot, sem erro de sintaxe e sem deploy novo.

DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(vector, double precision, integer);

NOTIFY pgrst, 'reload schema';

-- ============================================================================
-- Verificacao pos-aplicacao
-- ============================================================================
-- select count(*) from pg_proc where proname = 'buscar_produtos_semantico';  -- 1
-- select pg_get_function_identity_arguments(oid) from pg_proc
--  where proname = 'buscar_produtos_semantico';
--   -> query_embedding vector, match_threshold double precision,
--      match_count integer, filtro_tamanho text, filtro_id_loja text
-- Smoke: uma busca real do bot devolvendo linhas (nao so 200 vazio).
