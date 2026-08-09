-- Rollback da 0011: recria a sobrecarga legada de 3 args com o corpo EXATO
-- capturado por pg_get_functiondef(252201) antes do drop.
--
-- ATENCAO: aplicar isto devolve a AMBIGUIDADE. Enquanto as duas candidatas
-- existirem, qualquer chamada com 3 argumentos volta a falhar com
-- "Could not choose the best candidate function" / "is not unique".
-- A chamada do bot (5 chaves nomeadas) continua funcionando nos dois estados —
-- este rollback existe para o caso de aparecer um consumidor externo que precise
-- da assinatura antiga, nao por risco ao bot.

CREATE OR REPLACE FUNCTION public.buscar_produtos_semantico(query_embedding vector, match_threshold double precision, match_count integer)
 RETURNS TABLE(id_produto text, nome text, tamanho text, preco numeric, similarity double precision)
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

NOTIFY pgrst, 'reload schema';
