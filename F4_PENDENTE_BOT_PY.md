# F4 — o que ficou PENDENTE (e por que)

Frente 4 (Sync + Embedding) do `PLANO_CORRECAO_4_DEFEITOS.md`. O código de sync,
o módulo canônico de embedding e a migration `0012` **já estão no repo e no
banco**. Faltam duas coisas, cada uma bloqueada por um motivo diferente.

| Pendência | Bloqueio | Quem destrava |
|---|---|---|
| **A.** Bloco de `/health` + watchdog em `bot.py` (P4.19/P4.20) | O commit C1.4 da **Frente 1** reescreve o mesmo bloco `/health`. Aplicar agora dá conflito garantido | Fazer a F1 primeiro, depois colar o §A abaixo |
| **B.** Re-embed canônico em massa (P4.14-P4.17) | **O bot está no ar** (`bot_settings.is_active = true`) atendendo cliente real. O plano assumia bot desligado. São ~40-50 min reescrevendo a coluna que a busca usa | O dono escolhe a janela e roda o §B abaixo |

Também **não foi executado** o controle positivo em produção (P4.3/P4.5): ele
manda renomear um produto e somar +1 ao estoque de uma loja inteira, no código
**antigo**, de propósito, para ver os vetores serem apagados. Com o bot no ar
isso é sabotagem do estoque de um cliente real. O mesmo fato foi provado sem
risco em `tests/test_sync_upsert_homogeneo.py`, contra tabela de teste própria:
o lote heterogêneo apaga, o lote homogêneo preserva.

---

## §A — Bloco a inserir em `bot.py` DEPOIS do commit C1.4 da Frente 1

Nada aqui muda `healthy` nem o status HTTP do `/health` — **decisão explícita**:
qualidade de dado não pode causar restart de container no Railway. O canal é o
alarme.

### A.1 — Constantes, junto de `SYNC_STALE_ALERT_MIN` (hoje `bot.py:2008`)

```python
EMB_NULL_ALERT_MAX = int(os.getenv("EMB_NULL_ALERT_MAX", "20"))
_emb_alert_state = {"alerting": False, "last_alert_at": None}
_emb_health_cache = {"at": None, "data": {}}
```

Limiar 20 = folga para falha transitória de embedding. Depois que o sync novo
curar a base o regime normal é **0** (o ramo de re-embed repara sozinho a cada
ciclo).

### A.2 — Leitor com cache, junto de `_sync_age_min()` (hoje `bot.py:2014`)

```python
def _embeddings_health(ttl_seg=600):
    """Metrica de embeddings nulos via RPC embeddings_health() (migration 0012).

    Cache de 10 min: /health e sonda de liveness do Railway e nao pode virar uma
    query por probe. Em erro devolve o ultimo valor conhecido e NUNCA levanta —
    /health nao pode cair porque a metrica de qualidade de dado falhou.
    """
    now = datetime.now(timezone.utc)
    at = _emb_health_cache["at"]
    if at is not None and (now - at).total_seconds() < ttl_seg:
        return _emb_health_cache["data"]
    try:
        r = supabase.rpc("embeddings_health").execute()
        _emb_health_cache["data"] = (r.data or [{}])[0] or {}
        _emb_health_cache["at"] = now
    except Exception as e:
        print(f"[EMB] health falhou (mantendo ultimo valor): {e}")
    return _emb_health_cache["data"]
```

### A.3 — Três chaves no dict `body` do `/health` (hoje `bot.py:2028-2035`)

```python
        "embeddings": _embeddings_health(),
        "sync_emb_nulos_inicio": sync_erp.LAST_RUN_EMB_NULOS,
        "sync_reembeds": sync_erp.LAST_RUN_REEMBEDS,
```

`LAST_RUN_EMB_NULOS` e `LAST_RUN_REEMBEDS` já existem em `sync_erp.py` (commit
`95aaaf7`) e são `None` até o primeiro ciclo. **Fonte global é válida aqui**: o
Gate 0 confirmou serviço único, réplica única e `ENABLE_INPROCESS_SYNC` não
definida (default `"1"`, `bot.py:2103`) — o sync roda **no mesmo processo** do
web. Se um dia o cron dedicado (`railway.cron.json`) for ligado, estas duas
chaves passam a ser sempre `None` e a métrica real continua vindo de
`"embeddings"` (RPC), que independe do processo.

### A.4 — Segunda condição de alarme no watchdog do sync (hoje `bot.py:2072`)

O guard de zeragem nasce **mudo** sem isto: `_verificar_sync_e_alertar` lê
**somente** a idade do sync, e um ciclo degradado é recente, não velho.

```python
        degradado = (sync_erp.LAST_RUN_OK is False)
        if age > tol or degradado:
```

e no corpo da mensagem, acrescentar:

```python
                    f"Ultimo ciclo terminou {'DEGRADADO' if degradado else 'ok'}: {sync_erp.LAST_RUN_INFO}\n"
```

### A.5 — Job de alarme de embeddings, junto de `_verificar_sync_e_alertar`

```python
def _verificar_embeddings_e_alertar():
    """Alerta quando produtos com estoque estao invisiveis para a busca semantica.

    Mensagem COMERCIAL de proposito: quem le e o dono da loja, e o efeito visivel
    e "o cliente nao encontra esses produtos", nao "a coluna embedding esta NULL".
    """
    try:
        now = datetime.now(timezone.utc)
        h = _embeddings_health()
        nulos = h.get("nulos_com_estoque")
        if nulos is None:
            return                      # metrica indisponivel: nao alarma no escuro
        if nulos > EMB_NULL_ALERT_MAX:
            la = _emb_alert_state["last_alert_at"]
            reenvio = la is None or (now - la).total_seconds() > 12 * 3600
            if not _emb_alert_state["alerting"] or reenvio:
                corpo = (
                    f"{nulos} produtos COM ESTOQUE nao aparecem na busca da Luna "
                    "— o cliente pede e ela responde que nao tem.\n"
                    f"Campeoes de venda afetados: {h.get('nulos_mais_vendidos')}\n"
                    f"Produtos distintos: {h.get('produtos_com_nulo')} de {h.get('linhas_total')} linhas.\n"
                    "O sync repara sozinho ate 300 por ciclo; se este numero nao cair "
                    "em ~1h, rode: python regenerar_embeddings.py --only-missing"
                )
                _disparar_alerta("Produtos invisiveis na busca", corpo)
                _emb_alert_state["alerting"] = True
                _emb_alert_state["last_alert_at"] = now
                print(f"[WATCHDOG] alerta de embeddings enviado (nulos_com_estoque={nulos})")
        else:
            if _emb_alert_state["alerting"]:
                _disparar_alerta("Busca normalizada",
                                 f"Os produtos voltaram a aparecer na busca (restam {nulos}).")
                print("[WATCHDOG] embeddings normalizados")
            _emb_alert_state["alerting"] = False
    except Exception as e:
        print(f"[WATCHDOG] erro no alarme de embeddings: {e}")
```

### A.6 — Registrar o job (junto de `bot.py:2110`)

```python
    scheduler.add_job(_verificar_embeddings_e_alertar, 'interval', minutes=60,
                      misfire_grace_time=600, coalesce=True, max_instances=1)
```

### A.7 — Testar o alarme UMA vez (P4.20)

Subir com `EMB_NULL_ALERT_MAX=-1`, confirmar o recebimento no WhatsApp/e-mail e
**devolver a 20 no mesmo turno de trabalho**. Deixar em `-1` é alarme constante,
e alarme constante é alarme ignorado.

### A.8 — Commit

```
feat(health): metrica de embeddings nulos + alarme
```

---

## §B — Runbook do re-embed canônico total (o dono escolhe a janela)

Estado medido em 08/08/2026 (a mesma consulta serve de antes/depois):

```sql
select * from public.embeddings_health();
-- 6739 linhas | 1203 nulos | 979 nulos com estoque | 347 produtos | 428 campeoes de venda
```

Por que **total** e não `--only-missing`: a base está com convenção **mista**.
`--only-missing` por definição não toca linha não-nula, e ~40% das linhas
não-nulas foram escritas pelo `regenerar_embeddings.py` antigo com
`"<nome> | Tamanho: <t>"` (0,857-0,930 de cosseno contra o canônico, num ranking
cujo top-12 cabe em 0,018 de spread). Detectar qual está errada custa a mesma
chamada de API que corrigir.

### B.0 — Conferir que o script mergeado é o novo (senão o passo CONTAMINA a base)

```bash
git log -1 --stat regenerar_embeddings.py     # tem de mostrar o commit a145c2c ou posterior
python regenerar_embeddings.py --dry-run      # nao escreve nada
# esperado: 6739 linhas lidas | 1576 pares (id_produto, nome) | 1539 chamadas de API
```

### B.1 — Backup da coluna (é o **único** rollback do re-embed)

```sql
create table embedding_backup_20260808 as
  select id_unico, embedding from produtos_estoque where embedding is not null;
-- ~5.536 linhas, ~17 MB. Guardar ~2 semanas.
```

Rollback, se precisar:

```sql
update produtos_estoque p set embedding = b.embedding
  from embedding_backup_20260808 b where b.id_unico = p.id_unico;
```

**Atenção:** isso devolve **também** os vetores contaminados e não repõe os
NULLs. É volta ao estado ruim conhecido, não a um estado neutro.

### B.2 — Rodar (comando exato)

```bash
cd c:\Users\ThinkPad\Desktop\bot_sangali
.venv\Scripts\python.exe regenerar_embeddings.py 2>&1 | tee reembed_total.log
```

**Tempo estimado: 40-50 min.** 1.539 chamadas de API × ~1,3 s + 1.576 UPDATEs +
uma pausa de 5 s a cada 100 itens (≈ 80 s no total).

Se morrer no meio, **não restaure o backup**: retome de onde parou. A lista é
determinística (`order by id_produto, nome`), então:

```bash
.venv\Scripts\python.exe regenerar_embeddings.py --skip=<ultimo i impresso>
```

Já provado em produção, em 2 linhas (as únicas tocadas por esta frente):
`220540_10218165_54` (0,9292 → 1,0000) e `220540_10218253_M` (0,7943 → 1,0000).

### B.3 — REINDEX (obrigatório, não é higiene opcional)

```sql
REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx;
ANALYZE produtos_estoque;
```

HNSW no Postgres não recupera entradas mortas. Reescrever ~6,7 mil vetores deixa
metade do grafo morto e degrada recall e latência **no caminho que a RPC usa
hoje** (a de `0009` faz `ORDER BY embedding <=> v`, elegível ao índice).

### B.4 — Provas de saída

```sql
select * from public.embeddings_health();   -- nulos_com_estoque = 0 e nulos_mais_vendidos = 0
```

- **Convenção unificada:** sortear 10 `id_produto`, embedar o `nome` puro
  (`retrieval_document`, 768) e exigir similaridade **≥ 0,9999** em todos. Hoje
  ~40% devolve 0,857-0,930.
- **Recall:** rodar o mesmo top-12 de `'baby doll de renda'` (`RETRIEVAL_QUERY`)
  na forma aproximada (`order by embedding <=> v`) e na exata
  (`order by 1-(embedding <=> v) desc`) e exigir **conjuntos idênticos**.
  Divergência aponta índice degradado (repetir B.3), **não** regressão do texto
  canônico — não culpe o texto antes de rodar este check.
- `python tests/eval/run_eval.py` completo, 18/18. É a mudança com maior chance
  de mexer em cenário de curadoria.

### B.5 — Limpeza

```sql
drop table if exists _emb_watch;            -- nunca chegou a ser criada nesta rodada
drop table if exists public._test_emb_upsert;
```

`_test_emb_upsert` é a tabela de apoio de `tests/test_sync_upsert_homogeneo.py`.
Foi derrubada ao fim desta frente; para rodar o teste de novo, recrie com o DDL
que está no docstring do próprio arquivo de teste.
