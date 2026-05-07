-- ROLLBACK 0001: remove tamanho_tokens, trigger, função e índice
-- Aplicar APÓS rollback de 0002 (que dependia da função normalize_tamanho_tokens).

DROP TRIGGER IF EXISTS produtos_estoque_set_tamanho_tokens ON public.produtos_estoque;
DROP FUNCTION IF EXISTS public.trg_set_tamanho_tokens() CASCADE;
DROP INDEX IF EXISTS public.idx_produtos_estoque_tamanho_tokens;
ALTER TABLE public.produtos_estoque DROP COLUMN IF EXISTS tamanho_tokens;
DROP FUNCTION IF EXISTS public.normalize_tamanho_tokens(text) CASCADE;
