# Migrations

Aplicar **em ordem** via Supabase Dashboard → SQL Editor (uma de cada vez).

## Ordem

1. `0001_normalize_tamanho.sql` — coluna `tamanho_tokens`, função de normalização, trigger e índice GIN. Resolve Risco 4.
2. `0002_update_rpc_buscar_produtos_semantico.sql` — RPC passa a casar tamanho via tokens. **Pré-requisito:** 0001 aplicada.
3. `0003_bot_turns.sql` — tabela de logging estruturado por turno. Resolve Dimensão 7.
4. `0004_promocoes_ativas.sql` — tabela de Dia S e similares. Resolve Risco 3.
5. `0009_rpc_buscar_produtos_semantico_id_loja.sql` — adiciona `filtro_id_loja` na RPC. **Pré-requisito:** 0002 aplicada.
6. `0010_tool_filtro_eventos.sql` — tabela de observabilidade do filtro de tamanho da tool de busca (Frente 2). **Aplicada.** Inclui `purge_tool_filtro_eventos(dias)` para expurgo manual (backlog B14).
7. `0011_drop_rpc_buscar_produtos_semantico_3args.sql` — dropa a sobrecarga legada de 3 args (Frente 2). **Aplicada.** Depois dela `pg_proc` tem **1** candidata.
8. `0012_embeddings_health.sql` — função `embeddings_health()`: quantas linhas estão invisíveis para a busca semântica. Consumida pelo `/health` e pelo alarme (Frente 4).

## Numeração reservada (PLANO_CORRECAO_4_DEFEITOS.md §1)

Para não haver colisão entre as frentes em andamento:

| Nº | Frente | Conteúdo |
|---|---|---|
| **0010** | **F2** | **`tool_filtro_eventos` — aplicada** |
| **0011** | **F2** | **drop da sobrecarga legada de 3 args de `buscar_produtos_semantico` — aplicada** |
| **0012** | **F4** | **`embeddings_health()` — aplicada** |
| 0013 | F5 | RPC de ranking comercial |

## REGRA DURA — assinatura de `buscar_produtos_semantico`

**Nenhuma migration posterior à 0013 pode recriar a assinatura de 5 args de
`public.buscar_produtos_semantico`.** Com duas candidatas, o PostgREST responde
*"Could not choose the best candidate function"* à chamada nomeada de
`bot.py:360` e **toda** busca do bot cai — sem erro de sintaxe, sem deploy novo,
só busca vazia. Antes de qualquer `CREATE OR REPLACE` nessa função:

```sql
select count(*) from pg_proc where proname = 'buscar_produtos_semantico';  -- tem de ser 1
```

Se precisar mudar a assinatura, faça `DROP FUNCTION` da anterior **na mesma
migration** (é o que a 0009 faz) e termine com `NOTIFY pgrst, 'reload schema';`.

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

-- 0012: metrica de embeddings (tem de rodar tambem com a chave anon, que e a do bot)
SELECT * FROM public.embeddings_health();
```

## Notas

- **0002** assume que a função `buscar_produtos_semantico` já existe com a assinatura `(vector(768), float, int, text)`. Se a assinatura difere, faça `DROP FUNCTION buscar_produtos_semantico(...)` antes de aplicar.
- **0002** assume tipos de coluna: `id_produto bigint`, `id_loja bigint`, `preco numeric`, etc. Verifique com `\d produtos_estoque` se houver erro de tipo no `RETURNS TABLE` e ajuste antes de aplicar.
- **0003** sugere `pg_cron` para retenção automática de 30 dias. Se não usar pg_cron, rode `SELECT purge_old_bot_turns();` periodicamente (ex: scheduler do bot).
- **0004** seed só insere se não existir registro com `nome = 'Dia S'`. Categoria é placeholder — atualize por `UPDATE promocoes_ativas SET categoria = 'lingerie básica' WHERE nome = 'Dia S';` toda semana.
