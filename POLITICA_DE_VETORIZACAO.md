# Política de vetorização — nenhum produto invisível

## O invariante

> **Toda linha de `produtos_estoque` tem `embedding` não-nulo.**
> Corolário: nenhum produto com estoque é invisível para a Luna.

Não é higiene, é requisito funcional. A RPC de busca filtra por
`1 - (embedding <=> query_embedding) > match_threshold`. Com `embedding` NULL a
expressão é NULL, o predicado **nunca** é verdadeiro e a linha é **descartada em
silêncio** — sem erro, sem log, sem sintoma. O produto simplesmente não existe
para o robô: o cliente pede, e a Luna responde que não tem.

Foi assim que **41,5% do grupo "PRODUTOS MAIS VENDIDOS"** ficou fora da busca sem
ninguém notar, e a taxa de nulos **crescia com o estoque** (9,6% nas linhas
zeradas contra 60,8% na faixa de 15-19 unidades) — ou seja, atingia com mais
força exatamente o que a loja mais tem para vender.

---

## As cinco camadas

### 1. Prevenção — nada cria linha sem vetor de propósito

| Caminho | Garantia |
|---|---|
| Produto **novo** | `get_embedding` antes do insert (`sync_erp.py`, ramo `existente is None`) |
| **Nome mudou** | re-embed no mesmo ciclo (ramo `existente["nome"] != base_nome`) |
| Mudou só **estoque/preço** | registro entra **sem** a chave `embedding`, em **lote homogêneo** — ver abaixo |
| Backfill manual | `regenerar_embeddings.py` usa `update` com dict único |

**A regra dura que sustenta a coluna:** o `upsert` do sync agrupa por conjunto de
chaves (`lotes_homogeneos`). O `postgrest-py` monta `?columns=<união das chaves do
lote>`, então um lote misturando registros *com* e *sem* `embedding` grava **NULL**
em quem não traz a chave. Foi o mecanismo que apagou 1.203 vetores. Reproduzido em
tabela de teste e coberto por `tests/test_sync_upsert_homogeneo.py`.

> ⚠️ **`regenerar_embeddings.py` é imune ao bug porque usa `update` com dict
> único. NUNCA converter para `upsert` em lote.** Está escrito no cabeçalho do
> próprio script.

### 2. Detecção — o que escapa é encontrado sozinho

O único jeito de uma linha nascer sem vetor é **`get_embedding` falhar** (rede,
timeout, quota). Para isso existe o ramo de reparo:

- `carregar_ids_sem_embedding()` roda a cada ciclo (10 min) e lista **toda** linha
  com `embedding IS NULL`. **Não filtra por estoque** — de propósito: produto que
  volta ao estoque precisa estar vetorizado *antes*, não depois.
- O ramo `elif id_unico in ids_sem_embedding` re-embeda a linha, posicionado
  **depois** dos ramos de novo/renomeado e **antes** da comparação de
  estoque/preço (senão a linha cairia em `skipped` e ficaria nula para sempre).
- Cap de `SYNC_REEMBED_MAX=300` **chamadas** por ciclo (não linhas), com cache por
  texto. Contar linhas faria a cura levar 7 ciclos em vez de 1.

### 3. Visibilidade — o estado é consultável a qualquer momento

`GET /health` expõe:

```json
"embeddings": { "linhas_total": …, "nulos_total": …, "nulos_com_estoque": …,
                "produtos_com_nulo": …, "nulos_mais_vendidos": … },
"sync_emb_nulos_inicio": …,   // quantas o último ciclo viu
"sync_reembeds": …            // quantas ele reparou
```

**Regime normal de `nulos_com_estoque` é 0.** Qualquer valor persistente acima
disso é produto invisível, não ruído.

### 4. Alarme — alguém é avisado, em linguagem de negócio

`_verificar_embeddings_e_alertar()` a cada 60 min, limiar `EMB_NULL_ALERT_MAX=20`
(folga para falha transitória), reenvio a cada 12h, com aviso de normalização. A
mensagem é comercial de propósito — quem lê é o dono da loja:

> *"N produtos COM ESTOQUE não aparecem na busca da Luna — o cliente pede e ela
> responde que não tem."*

### 5. Convenção — o texto embedado tem fonte única

`embedding_text.py` → `texto_embedding_produto(nome)`. Sync e regenerador usam a
**mesma** função. Não é preciosismo: quando as duas convenções divergiam
(`base_nome` contra `nome | Tamanho: X`), o vetor caía **0,09-0,11 de cosseno**
fora do lugar — num ranking cuja diferença entre o 1º e o 20º colocado é
**0,02**. Vetor com convenção errada é pior que vetor ausente, porque não aparece
em nenhuma métrica de nulos.

---

## Lacunas conhecidas (honestas)

### a) Falha **persistente** de `get_embedding` não é distinguida de "curando"

Aconteceu hoje: a chave do Gemini bateu o **teto de gastos mensal**,
`get_embedding` passou a devolver `None`, e qualquer produto novo entraria sem
vetor. O alarme detecta — mas só depois de 20 linhas acumuladas, e a mensagem diz
"o sync repara sozinho", que naquele momento era **falso**.

**Proposta:** alarmar também por **N falhas consecutivas de `get_embedding` no
mesmo ciclo**, independente da contagem de nulos. É a diferença entre *"está
curando devagar"* e *"está travado"*. Não implementado.

### b) Loja fora do sync não é reparada

`carregar_ids_sem_embedding()` acha a linha nula, mas o ramo de reparo só roda
para linhas que o **ERP devolveu** naquele ciclo. Produto descontinuado tem
estoque zerado pelo próprio sync e sai da RPC — ok. A exceção é **SÃO MATEUS**:
538 linhas com estoque de uma loja que o ERP **não sincroniza mais** (hoje ele a
chama `LIMP +`). Hoje são 0 nulos ali, mas se ficarem, ninguém repara.
**Depende da decisão §10.3 item 9** do plano: propagar o nome novo, excluir a loja
do sync, ou zerar o estoque.

### c) Janela do próprio backfill

Enquanto o re-embed roda, **produção está com o código antigo** — que ainda apaga
vetores em lote heterogêneo. Risco baixo (os ciclos recentes mostram
`upserts:0 embeds_novos:0`, ou seja o ERP não está mudando nada), mas real: um
produto novo cadastrado durante a janela pode apagar vetores recém-escritos.
**Fecha ao deployar** o fix do sync. Até lá, a verificação de fechamento (abaixo)
é obrigatória.

---

## Runbook do re-embed total

Quando usar: convenção de texto mudou, ou `nulos_com_estoque` não cai sozinho.
Para só tapar nulos, `--only-missing` basta e é muito mais rápido.

```bash
# 0. conferir que o script mergeado e o novo (senao CONTAMINA a base)
git log -1 --stat regenerar_embeddings.py           # >= a145c2c
.venv/Scripts/python.exe regenerar_embeddings.py --dry-run
#    esperado hoje: 6739 linhas | 1576 pares | 1539 chamadas de API

# 1. backup — e o UNICO rollback
#    create table embedding_backup_<data> as
#      select id_unico, embedding from produtos_estoque where embedding is not null;

# 2. rodar (~40-50 min)
.venv/Scripts/python.exe -u regenerar_embeddings.py 2>&1 | tee reembed_total.log
#    morreu no meio? NAO restaure: retome com --skip=<ultimo i impresso>

# 3. REINDEX (obrigatorio — HNSW nao recupera entrada morta)
#    REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx;
#    ANALYZE produtos_estoque;

# 4. FECHAMENTO — o invariante tem de valer
#    select * from public.embeddings_health();
#    -> nulos_com_estoque = 0  E  nulos_mais_vendidos = 0
```

O rollback devolve **também** os vetores de convenção antiga e **não** repõe os
NULLs: é volta ao estado ruim conhecido, não a um estado neutro. Use só se o
re-embed tiver piorado a busca de forma medida.

---

## Como provar que o invariante vale

Não basta rodar o backfill uma vez. A prova é em três tempos:

1. **Agora:** `nulos_com_estoque = 0` depois do re-embed + REINDEX.
2. **Um ciclo depois:** rodar o sync e confirmar que `nulos_com_estoque` **continua**
   0 — prova que a escrita não apaga mais (é o teste que o
   `tests/test_sync_upsert_homogeneo.py` faz em laboratório, agora em produção).
3. **Contínuo:** `/health` e o alarme de 60 min. Se o número subir e não cair no
   ciclo seguinte, a camada 2 falhou e é bug, não ruído.

O que **não** serve como prova: contar nulos logo após o backfill sem rodar um
ciclo de sync depois. Era exatamente esse o cenário que fazia o defeito voltar
"com o item marcado como concluído".
