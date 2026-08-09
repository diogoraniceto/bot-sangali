-- Migration 0010: tool_filtro_eventos — o que a tool de busca REALMENTE filtrou.
-- Frente 2. Numeracao reservada no PLANO_CORRECAO_4_DEFEITOS.md (§1):
--   F2 = 0010 e 0011 · F4 = 0012 · F5 = 0013.
--
-- Para que serve: medir os defeitos que hoje sao invisiveis —
--   (a) `n_dropados_igualdade_legado > 0` = um falso "nao tenho nesse tamanho" que
--       o codigo pre-F2 teria produzido (UNICO derrubava 623 de 674 linhas);
--   (b) `guard_acionou` = o LLM passou um tamanho que o cliente nao escreveu;
--   (c) `llm_omitiu_tamanho` = o cliente deu tamanho e o LLM NAO passou (modo de
--       falha mais provavel do defeito P3, hoje sem nenhum instrumento);
--   (d) `tamanhos_dropados` = insumo para decidir o backlog B3 (vocabulario de
--       `_tamanhos_validos_na_msg`: EG, 2EG, XL, G4) COM dado em vez de palpite.
--
-- RLS OFF + grant a anon NAO e descuido: o bot usa a chave anon (JWT role=anon) e
-- todas as tabelas atuais (bot_turns, card_envios, chat_history,
-- conversation_handoffs) seguem esse padrao. RLS sem policy faria o INSERT falhar
-- dentro do try/except de `_log_filtro_evento` e a observabilidade sumiria em
-- silencio — o pior resultado possivel para uma tabela de diagnostico.
--
-- APPEND-ONLY e com DUPLICATA ESPERADA: o modelo chama a tool varias vezes por
-- turno por decisao propria (observado: 3 chamadas em 9,4 s) e o retry de 3
-- tentativas do Gemini replaya as tool calls. Toda metrica tem de AGRUPAR POR
-- TURNO (user_id + janela de created_at), nunca contar linhas cruas.
--
-- Retencao: sem politica automatica nesta rodada (backlog B14). `purge_tool_filtro_eventos`
-- abaixo existe para ser chamada a mao ou por scheduler quando o dono decidir o prazo.

CREATE TABLE IF NOT EXISTS public.tool_filtro_eventos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  user_id text,
  tool text NOT NULL DEFAULT 'consultar_estoque_supabase',
  termo_cliente text,
  tamanho_llm text,                       -- o que o modelo passou
  tamanho_user_tokens text[],             -- o que o CLIENTE escreveu (texto saneado)
  tamanho_aplicado text,                  -- o que a busca usou de fato
  tamanho_aplicado_tokens text[],
  guard_acionou boolean NOT NULL DEFAULT false,
  llm_omitiu_tamanho boolean NOT NULL DEFAULT false,
  id_loja_aplicado text,
  n_candidatos integer,                   -- linhas que a RPC devolveu
  n_validados integer,                    -- sobreviveram ao overlap
  n_dropados_overlap integer,
  n_dropados_igualdade_legado integer,    -- overlap salvou; a igualdade mataria
  tamanhos_dropados text[],
  status_retornado text
);

CREATE INDEX IF NOT EXISTS idx_tfe_created_at ON public.tool_filtro_eventos (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tfe_guard ON public.tool_filtro_eventos (guard_acionou) WHERE guard_acionou;

ALTER TABLE public.tool_filtro_eventos DISABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON public.tool_filtro_eventos TO anon, authenticated, service_role;

-- Expurgo manual (backlog B14 decide o prazo). Devolve quantas linhas apagou.
CREATE OR REPLACE FUNCTION public.purge_tool_filtro_eventos(dias integer DEFAULT 90)
RETURNS integer
LANGUAGE plpgsql
AS $function$
DECLARE n integer;
BEGIN
  DELETE FROM public.tool_filtro_eventos
   WHERE created_at < now() - (dias || ' days')::interval;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.purge_tool_filtro_eventos(integer) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- ============================================================================
-- Verificacao pos-aplicacao
-- ============================================================================
-- SELECT count(*) FROM public.tool_filtro_eventos;                 -- 0, sem erro
--
-- Metricas (SEMPRE com o filtro de eval: a suite de conformidade escreve aqui):
--   -- falsos "nao tenho" que o codigo pre-F2 teria produzido, POR TURNO:
--   select date_trunc('minute', created_at) turno, user_id,
--          max(n_dropados_igualdade_legado) salvas, array_agg(distinct tamanho_aplicado)
--     from public.tool_filtro_eventos
--    where user_id not like 'eval_%' and n_dropados_igualdade_legado > 0
--    group by 1,2 order by 1 desc;
--
--   -- o LLM omitindo o tamanho que o cliente deu (defeito P3):
--   select count(distinct (user_id, date_trunc('minute', created_at)))
--     from public.tool_filtro_eventos
--    where user_id not like 'eval_%' and llm_omitiu_tamanho;
--
--   -- vocabulario que falta ao guard (backlog B3), com dado:
--   select t, count(*) from public.tool_filtro_eventos,
--          unnest(tamanhos_dropados) t where user_id not like 'eval_%'
--    group by 1 order by 2 desc;
