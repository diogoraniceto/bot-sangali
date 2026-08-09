-- Rollback da 0010. Derrubar a tabela NAO quebra o bot: `_log_filtro_evento` e
-- try/except que so imprime (bot.py). Perde-se a observabilidade do filtro de
-- tamanho, nada mais. Nenhuma outra tabela referencia esta.
DROP FUNCTION IF EXISTS public.purge_tool_filtro_eventos(integer);
DROP TABLE IF EXISTS public.tool_filtro_eventos;

NOTIFY pgrst, 'reload schema';
