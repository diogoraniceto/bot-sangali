# Migrations

Aplicar **em ordem** via Supabase Dashboard → SQL Editor (uma de cada vez).

## Ordem

1. `0001_normalize_tamanho.sql` — coluna `tamanho_tokens`, função de normalização, trigger e índice GIN. Resolve Risco 4.
2. `0002_update_rpc_buscar_produtos_semantico.sql` — RPC passa a casar tamanho via tokens. **Pré-requisito:** 0001 aplicada.
3. `0003_bot_turns.sql` — tabela de logging estruturado por turno. Resolve Dimensão 7.
4. `0004_promocoes_ativas.sql` — tabela de Dia S e similares. Resolve Risco 3.
5. `0009_rpc_buscar_produtos_semantico_id_loja.sql` — adiciona `filtro_id_loja` na RPC. **Pré-requisito:** 0002 aplicada.

## Verificação rápida pós-aplicação

```sql
-- 0001: tokens populados?
SELECT tamanho, tamanho_tokens FROM produtos_estoque LIMIT 10;

-- 0002: RPC funciona com tamanho não normalizado?
SELECT count(*) FROM buscar_produtos_semantico(
  (SELECT embedding FROM produtos_estoque LIMIT 1),
  0.3, 5, '42'
);

-- 0003: tabela existe?
SELECT count(*) FROM bot_turns;

-- 0004: Dia S seed?
SELECT * FROM promocoes_ativas;
```

## Notas

- **0002** assume que a função `buscar_produtos_semantico` já existe com a assinatura `(vector(768), float, int, text)`. Se a assinatura difere, faça `DROP FUNCTION buscar_produtos_semantico(...)` antes de aplicar.
- **0002** assume tipos de coluna: `id_produto bigint`, `id_loja bigint`, `preco numeric`, etc. Verifique com `\d produtos_estoque` se houver erro de tipo no `RETURNS TABLE` e ajuste antes de aplicar.
- **0003** sugere `pg_cron` para retenção automática de 30 dias. Se não usar pg_cron, rode `SELECT purge_old_bot_turns();` periodicamente (ex: scheduler do bot).
- **0004** seed só insere se não existir registro com `nome = 'Dia S'`. Categoria é placeholder — atualize por `UPDATE promocoes_ativas SET categoria = 'lingerie básica' WHERE nome = 'Dia S';` toda semana.
