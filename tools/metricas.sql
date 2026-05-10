-- Métricas operacionais do bot — query SQL pronta para o Supabase Dashboard.
-- Custos Gemini Flash (referência aproximada, ajuste se Google mudar tabela):
--   Input  : $0.075 / 1M tokens
--   Output : $0.30  / 1M tokens
-- Tokens em produção podem variar; valores em USD.

-- 1. Resumo dos últimos 7 dias
SELECT
    date_trunc('day', created_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
    count(*) AS turnos,
    count(DISTINCT user_id) AS clientes_unicos,
    count(*) FILTER (WHERE output_format = 'json')   AS turnos_json,
    count(*) FILTER (WHERE fallback_used)            AS fallback_count,
    count(*) FILTER (WHERE error IS NOT NULL)        AS erros,
    round(avg(latency_ms)::numeric, 0)               AS lat_media_ms,
    percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS lat_p95_ms,
    sum(coalesce(tokens_in, 0))                      AS tokens_in,
    sum(coalesce(tokens_out, 0))                     AS tokens_out,
    round(
        (sum(coalesce(tokens_in,0))  * 0.075 / 1000000.0 +
         sum(coalesce(tokens_out,0)) * 0.30  / 1000000.0)::numeric, 4
    ) AS custo_usd_estimado
FROM bot_turns
WHERE created_at > now() - INTERVAL '7 days'
  AND user_id NOT LIKE 'test_%'
GROUP BY 1
ORDER BY 1 DESC;

-- 2. Top 10 clientes mais ativos (últimas 24h)
SELECT user_id, count(*) AS turnos, sum(latency_ms) AS lat_total_ms,
       sum(coalesce(tokens_in,0) + coalesce(tokens_out,0)) AS tokens
FROM bot_turns
WHERE created_at > now() - INTERVAL '24 hours'
  AND user_id NOT LIKE 'test_%'
GROUP BY user_id
ORDER BY turnos DESC
LIMIT 10;

-- 3. Turnos com fallback regex (sinal de saúde do JSON contract)
SELECT created_at, user_id, user_input, final_output, error
FROM bot_turns
WHERE fallback_used = true
  AND created_at > now() - INTERVAL '7 days'
ORDER BY created_at DESC
LIMIT 50;

-- 4. Tools mais chamadas (últimas 24h)
WITH calls AS (
    SELECT
        jsonb_array_elements(tool_calls) AS evt,
        user_id, created_at
    FROM bot_turns
    WHERE created_at > now() - INTERVAL '24 hours'
      AND user_id NOT LIKE 'test_%'
)
SELECT
    evt->>'name' AS tool_name,
    count(*) AS chamadas,
    count(DISTINCT user_id) AS clientes
FROM calls
WHERE evt->>'kind' = 'call'
GROUP BY tool_name
ORDER BY chamadas DESC;

-- 5. Handoffs por motivo (últimos 7 dias)
SELECT motivo, count(*) AS qtd
FROM conversation_handoffs
WHERE created_at > now() - INTERVAL '7 days'
GROUP BY motivo
ORDER BY qtd DESC;
