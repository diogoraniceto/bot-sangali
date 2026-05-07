-- ROLLBACK 0003: remove tabela bot_turns e função de purge
DROP FUNCTION IF EXISTS public.purge_old_bot_turns();
DROP TABLE IF EXISTS public.bot_turns;
