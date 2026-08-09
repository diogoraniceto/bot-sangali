-- Rollback da 0012. O /health degrada devolvendo "embeddings": {} (o consumidor
-- e try/except com cache) e o alarme de embeddings para de disparar. Nada mais.
DROP FUNCTION IF EXISTS public.embeddings_health();

NOTIFY pgrst, 'reload schema';
