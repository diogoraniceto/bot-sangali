# PLANO FINAL DE EXECUÇÃO — Bot Luna / Sangali

Consolida as 5 frentes revisadas + o deploy único de prompt. **Onde o revisor reprovou, o plano abaixo já é a versão corrigida** — cada correção está marcada `[AJUSTADO]`. Divergências decididas contra o revisor estão marcadas `[DECISÃO]` com a razão. Itens que dependem do dono da loja estão marcados `[BLOQUEADO]`.

Convenções: **`CÓDIGO`** = arquivo do repo · **`DDL`** = migration no Supabase · **`PROMPT`** = `bot_settings.system_prompt` id=1 via `inserir_prompt.py` · **`DADO`** = escrita em massa na tabela.

---

## GATE 0 — Pré-voo operacional (30 min, antes de qualquer commit)

Sem isso, três frentes ficam sem prova e uma vira bug.

| # | Verificar | Por que trava | Ação se falhar |
|---|---|---|---|
| 0.1 | Serviço web do bot no Railway com **replicas = 1** e `Procfile` = `web: python bot.py` (sem gunicorn `-w>1`) | Locks, `message_buffers` e o registro de fotos vistas são memória **do processo**. Com 2 réplicas a F1 não serializa nada e a F3 mente ("são todas que eu tenho") | F1 e F3 param; a solução vira Redis (backlog) |
| 0.2 | Quem roda o sync: `ENABLE_INPROCESS_SYNC` (bot.py:2103) **ou** o cron dedicado (`railway.cron.json`, `restartPolicyType: NEVER`) | Se é o cron, as globais `LAST_RUN_*` (sync_erp.py:49-51) **nunca existem** no processo web → o watchdog atual alerta em falso e a métrica da F4 tem de vir de RPC, não de global | F4 passo 9b/17 muda de fonte (já previsto abaixo) |
| 0.3 | Versões reais de `supabase`/`postgrest`/`httpx` — ler no **log de build do nixpacks** (não há shell no container; `pip freeze` remoto é inviável) `[AJUSTADO]` | `requirements.txt:5` traz `supabase` sem pin. O bug **e** o fix da F4 vivem no `?columns=<união das chaves>` do postgrest-py | Pinar antes do fix (F4 passo 1) |
| 0.4 | `.env` na raiz com `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY` + `pip install -r requirements.txt` num venv local | `tests/eval` bate em Supabase e Gemini **reais** (só WhatsApp é stubbed). Hoje `import supabase` falha no Python local → **nenhum gate roda** | Bloqueia tudo |
| 0.5 | Estado do bot: conta banida, `WHATSAPP_PROVIDER=cloud`, `bot_settings.is_active` | **Todo smoke em produção fica pendente de religar.** Os gates desta rodada são eval + testes de unidade + SQL | Ver §3.4 |

**Consequência do 0.5, assumida no plano:** com o bot desligado (a) a janela para o re-embed total da F4 deixa de existir como problema — roda agora; (b) os SQL "24-48h com tráfego real" viram checklist de pós-religamento; (c) `tool_filtro_eventos` e `bot_turns` só terão tráfego da suíte, então **todo SQL de métrica precisa filtrar `user_id not like 'eval_%'`** `[AJUSTADO]`.

---

## 1. SEQUÊNCIA DE EXECUÇÃO

| Ordem | Frente | Objetivo | Esforço | O que destrava | GATE de saída (tem de passar antes de seguir) |
|---|---|---|---|---|---|
| **F1** | Hardening do turno `CÓDIGO` | Fim do reenvio de cards, do cross-talk entre clientes e da mensagem entregue 2x ao Gemini | médio (1-2 d) | Pré-requisito **duro** de F2 (`aviso` que cita a mensagem do cliente), F3 (turno longo) e F5 (estado de "já mostrado") | `tests/test_turn_hardening.py` T0-T4 verdes + `tests/test_scenarios.py` + `tests/test_cloud_migration.py` + `run_eval.py` sem falha grave nova vs baseline |
| **F2** | Verdade no retorno das tools `CÓDIGO`+`DDL` | Tool declara o filtro aplicado; re-filtro por overlap de tokens; mata a RPC legada de 3 args | médio (1-2 d) | Pré-requisito de F5 (dedupe pode eleger linha `P/M`) e F3 (mesmas funções) | `tests/test_tamanho_tokens.py` verde + os 2 checks novos falhando no código antigo e passando no novo + `pg_proc` com 1 sobrecarga |
| **F3** | Fotos sob demanda `CÓDIGO` | `n_fotos` nas 2 tools, ordem determinística, tool `mostrar_fotos_produto` | médio (1-2 d) | Fecha P4 **só depois** do prompt (F6) | `t_fotos.py` A-F verdes (inclui `ja_vistas: 1` após o card) + `extra_photos_sent` pass |
| **F4** | Sync + embedding `CÓDIGO`+`DDL`+`DADO` | Para de apagar vetor, repara sozinho, unifica a convenção de texto, refaz a base | médio-alto (2-3 d, ~45 min de janela de escrita) | Pré-requisito **duro** de F5 (48,7% dos bestsellers invisíveis) | Controle positivo pré/pós-fix + `nulos_com_estoque = 0` + `run_eval.py` 18/18 pós re-embed |
| **F5** | Ranking comercial `DDL`+`CÓDIGO` | 8 produtos distintos, bestseller **escopado por categoria**, `excluir_ids` | médio (1-2 d) | Fecha P1 e a variedade do "tem mais?" | `test_ranking_comercial.py` A1-A8 + eval e2e (`fantasia`+G sem calcinha em `produtos_recomendados`) |
| **F6** | Deploy único de prompt `PROMPT` | Corrige a afirmação falsa sobre bestsellers, ativa fotos/handoff/variedade/`filtro_aplicado` | baixo (2-4 h) | Fecha P4 de fato | Backup do prompt atual + `run_eval.py` completo pós-deploy + dump de volta em `prompt_luna_v2.txt` |

**Paralelismo permitido** `[DECISÃO]`: **F4 pode começar junto com F1**, porque só toca `sync_erp.py`, `regenerar_embeddings.py`, `embedding_text.py` e migrations — **exceto** seu último commit (`/health` + watchdog em `bot.py`), que só entra **depois** do commit C1.4 de F1 (colidem no mesmo bloco `/health`). Nenhuma outra frente pode editar `bot.py` em paralelo: F1 C1.1 reescreve exatamente as regiões que F2/F3/F5 editam depois.

**Renumeração de migrations** `[AJUSTADO — colisão real entre os planos]`: F2=`0010` (tool_filtro_eventos) e `0011` (drop RPC legada 3 args); F4=`0012` (embeddings_health); F5=`0013` (rpc ranking comercial). O plano original da F4 e da F5 pediam `0010`/`0012` — ficaria colisão. **Regra escrita em `migrations/README.md`: nenhuma migration posterior à 0013 pode recriar a assinatura de 5 args de `buscar_produtos_semantico`** — duas candidatas fazem o PostgREST responder *"Could not choose the best candidate function"* à chamada nomeada de bot.py:360 e **toda** busca cai.

---

## 2. FRENTE 1 — HARDENING DO TURNO `CÓDIGO`

### Correções aplicadas ao plano original
- `[AJUSTADO — ORDEM ERRADA]` O contexto de thread (`_turn_ctx`, `_set_turn_ctx`, `_clear_turn_ctx`, `_ctx_last_msg`, `_ctx_user_id`) nasce **no commit C1.1**, antes do wrapper. O plano original o criava no commit 2 e o invocava no commit 1 → `NameError` em 100% dos turnos, com T1/T2 e os 18 cenários falhando pelo motivo errado. **E** o C1.1 **mantém** as duas atribuições globais de hoje (bot.py:1328-1329), para o guard de bot.py:336 continuar idêntico; só o C1.2 as remove. Assim nenhum commit é sozinho inconsistente.
- `[AJUSTADO — AJUSTE PRINCIPAL (b)]` `try/except` obrigatório em volta de `_executar_turno` no wrapper: `save_message` (bot.py:213) roda **fora** do try interno e, com o drain no início, uma falha de rede ali passaria a **descartar a mensagem do cliente em silêncio** (hoje ela sobrevive no buffer e é recuperada — vazamento funcionava como retry acidental).
- `[AJUSTADO — LACUNA]` A cauda do history também é normalizada (não só a cabeça): 318 linhas `user` após `user` no banco continuariam entregando dois turnos `user` adjacentes ao Gemini — a forma exata que gerou o *"não temos GG"*. A afirmação original "o history termina em turno `model`" era falsa contra o dado.
- `[AJUSTADO]` `get_history` busca `limit + 1` quando `excluir_id` é informado (contexto efetivo continua 30, não 29) — remove por construção o único risco de regressão multi-turno.
- `[AJUSTADO]` O `except` de `_executar_turno` passa a gravar `save_message(user_id,"model",fallback)`, fechando o par user/model. Hoje todo turno com erro deixa linha `user` órfã **e** o fallback pede reenvio → o cliente reenvia e nasce o par `user`/`user` idêntico (4 já existem no banco).
- `[AJUSTADO]` `turnos_conhecidos` no `/health` é rotulado como **contador de clientes distintos desde o boot**, não alarme (só cresce). O alarme é `buffers_pendentes`.
- `[AJUSTADO]` A prova pré-fix (T0) não depende de `_executar_turno` (que não existe antes do refactor) — é asserção pura sobre `message_buffers`.
- `[AJUSTADO]` Gates incluem `tests/test_scenarios.py` e `tests/test_cloud_migration.py`: as duas dirigem o turno por `message_buffers` + `process_and_respond` e não apareciam em gate nenhum.
- `[NOTA]` `_montar_mensagem_operador` (bot.py:922) é o **segundo** chamador de `get_history` (`limit=8`). Não quebra (default `excluir_id=None` e o map final `{role, parts}` são preservados), e é mais uma razão para **não** mover `save_message`: o resumo enviado à atendente lê a mensagem atual do próprio `chat_history` no meio do turno.

### Passos

**P1.0 — Baseline (obrigatório, antes de tocar em uma linha).** `python tests/eval/run_eval.py` na HEAD → renomear o CSV para `tests/eval/baseline_frente1_antes.csv`. Anotar pass/fail por cenário e a lista de FALHAS GRAVES. Os CSVs existentes são de 2026-07-14, **anteriores a 3 commits** (`bb41e53`, `61dc49a`, `5e4f727`) — "baseline 100%" não está provado na HEAD. Cenário que já falhe hoje = falha pré-existente, **não** regressão desta frente.

**P1.1 — T0: provar o bug ANTES do fix** (`tests/test_turn_hardening.py`, novo). Cabeçalho igual a `tests/eval/run_eval.py:41-56` (`os.environ["ENABLE_INPROCESS_SYNC"]="0"` **antes** de `import bot`, `sys.path` para a raiz, `load_dotenv`), stubs de `enviar_mensagem_whatsapp`/`enviar_midia_whatsapp` como em run_eval.py:64-78. T0 não toca em `_executar_turno`:
```python
# T0 (roda no codigo de HOJE e no novo)
uid = "test_hard_pre"
bot.message_buffers[uid] = {"text": "oi", "timer": None}
th = threading.Thread(target=bot.process_and_respond, args=(uid,)); th.start()
time.sleep(0.3)
bot._enqueue_user_message(uid, "tem mais?")
# HOJE: message_buffers[uid]["text"] == "oi tem mais?"  <- texto ja respondido volta ao turno seguinte
# DEPOIS: == "tem mais?"
```

**P1.2 — Contexto de thread + lock de turno.** `bot.py:73` (depois de `_user_locks = {}`) e `bot.py:96` (depois de `_get_user_lock`):
```python
_user_turn_locks = {}   # threading.Lock() por user_id: serializa o TURNO INTEIRO (segurado por dezenas de segundos)
def _get_turn_lock(user_id):
    """Lock do TURNO. NUNCA adquirir na thread do webhook Flask.
    ORDEM DE AQUISICAO: turn_lock -> buffer_lock; nunca o inverso."""
    # mesmo corpo de _get_user_lock, usando _user_turn_locks

_turn_ctx = threading.local()   # contexto do turno NA THREAD atual (cada turno roda em sua Timer thread)
def _set_turn_ctx(user_id, texto):
    _turn_ctx.user_id = user_id; _turn_ctx.last_msg = texto; _turn_ctx.excluir_ids = []
def _clear_turn_ctx():
    _turn_ctx.user_id = None; _turn_ctx.last_msg = None; _turn_ctx.excluir_ids = []
def _ctx_last_msg():  return getattr(_turn_ctx, "last_msg", None)   # None fora de um turno
def _ctx_user_id():   return getattr(_turn_ctx, "user_id", None)    # gancho de F2 (log) e F3 (tool de fotos)
```
Atualizar a docstring de `_get_user_lock` (bot.py:89-90) para *"Lock CURTO do buffer (só mutação de `message_buffers`). Adquirido pela thread do webhook — proibido segurar por I/O."* **PROIBIDO** reusar `_get_user_lock` para o turno: `_enqueue_user_message` (bot.py:1832) o adquire na thread do webhook Flask; segurar 30-80s estouraria o timeout da Meta e causaria reentrega.

**P1.3 — Extrair `_executar_turno`.** `bot.py:1320-1493`. Nova assinatura já preparada para F3: `def _executar_turno(user_id, texto_completo, fotos_pendentes=None):`. Corpo = `save_message(...)` (hoje 1331) + todo o `try:` (1333) até o fim do `except` (1488), **verbatim**. Remover `global _consultar_estoque_active_user_id` (1321). **Apagar** 1490-1493 (`_get_user_lock` / `with` / `message_buffers.pop`) — o pop vai para o início. Docstring: *"Corpo do turno: config → Gemini (tools) → render → log. NÃO mexe em buffer nem em lock; assume turno já serializado e contexto de thread já setado."* Os 3 `return` antecipados (1348/1353/1359) passam a retornar de `_executar_turno` — é exatamente o que faz o `finally` do wrapper rodar sempre.

**P1.4 — Wrapper `process_and_respond`** (novo corpo em bot.py:1320):
```python
def process_and_respond(user_id):
    turn_lock = _get_turn_lock(user_id)
    buf_lock  = _get_user_lock(user_id)      # resolver AMBOS antes de adquirir qualquer um
    t0 = time.perf_counter()
    with turn_lock:
        espera_ms = int((time.perf_counter() - t0) * 1000)
        with buf_lock:
            buffer = message_buffers.pop(user_id, None)
        if buffer is None:                    # 'is None': dict drenado e sempre truthy
            print(f"[TURNO] {user_id}: buffer vazio (ja coalescido por outro turno)"); return
        tmr = buffer.get("timer")
        if tmr is not None:
            try: tmr.cancel()
            except Exception: pass
        texto_completo = (buffer.get("text") or "")
        if not texto_completo.strip():
            print(f"[TURNO] {user_id}: texto vazio — ignorando"); return
        print(f"[TURNO] {user_id}: espera_lock={espera_ms}ms | chars={len(texto_completo)}")
        print(f"DEBUG INPUT: '{texto_completo}'")
        _user_last_msg[user_id] = texto_completo          # C1.1 MANTEM (guard de bot.py:336 intacto); C1.2 remove
        _consultar_estoque_active_user_id = user_id       # idem  (declarar global no topo da funcao no C1.1)
        _set_turn_ctx(user_id, texto_completo)
        fotos_pendentes = []                              # F3 usa; no C1.1 fica vazio e inerte
        turno_ok = False
        try:
            _executar_turno(user_id, texto_completo, fotos_pendentes)
            turno_ok = True
        except Exception as e:
            traceback.print_exc()
            enviar_mensagem_whatsapp(user_id, "Ops, tive um probleminha tecnico aqui 😅 Pode reenviar sua ultima mensagem, por favor?")
            try: log_turn(user_id, texto_completo, [], "", 0, "", "error", True, error=str(e))
            except Exception: pass
        finally:
            _clear_turn_ctx()
        # F3 pluga aqui (ver P3.8). turno_ok evita foto orfa depois de fallback.
```
**Por que `turn_lock` ANTES do drain:** o turno B fica bloqueado com o buffer ainda cheio, então a msg3 que chegar entra no **mesmo** buffer e B drena "msg2 msg3" numa tacada; o timer da msg3 acorda depois, acha vazio e volta. Drenar antes de bloquear geraria duas respostas separadas.

**P1.5 — Timer daemon.** `bot.py:1847-1849`: inserir `t.daemon = True` entre a criação e `message_buffers[user_id]["timer"] = t`. `[AJUSTADO]` A justificativa correta **não** é o redeploy do Railway (sem handler de SIGTERM o processo morre na hora): é que o T0/T1 chamam `_enqueue_user_message`, que cria um Timer de 10s e **penduraria o processo de teste**.

**P1.6 — T1/T2** (`tests/test_turn_hardening.py`). `_orig = bot._executar_turno` **salvo antes** de qualquer stub `[AJUSTADO]`.
- **T1 (corrida):** `vistos=[]`; `bot._executar_turno = lambda uid, txt, fp=None: (vistos.append(txt), time.sleep(1.5))`; buffer de `test_hard_race` = "oi"; thread em `process_and_respond`; `sleep(0.3)`; `_enqueue_user_message("test_hard_race","tem mais?")`; join; `process_and_respond` de novo. **ASSERT `vistos == ["oi","tem mais?"]`** (hoje: `["oi","oi tem mais?"]` = os mesmos cards reenviados).
- **T2 (vazamento):** restaurar `bot._executar_turno = _orig`; **stubar também a leitura de config** `[AJUSTADO]` (com `is_active=false` o turno retorna em 1352 e o teste **nunca** percorre o `return` de 1359 que ele afirma provar); `bot._em_silencio_pos_handoff = lambda uid: True`; buffer de `test_hard_leak`; chamar. **ASSERT `"test_hard_leak" not in bot.message_buffers`** e nada enviado.
- `finally` do arquivo: `bot.supabase.table("chat_history").delete().like("user_id","test_hard%").execute()`.

**P1.7 — Gate do C1.1.** `python tests/test_turn_hardening.py` + `python tests/test_scenarios.py` + `python tests/test_cloud_migration.py` `[AJUSTADO]` + `run_eval.py --no-judge` + `run_eval.py` completo vs baseline. Compatibilidade do harness verificada: run_eval.py:512 seta `message_buffers[uid]` e chama `process_and_respond`; o drain consome essa entrada, e nenhum cenário depende dela sobreviver (run_eval.py:510-546 reescreve por turno).

**P1.8 — Remover os globais.** `bot.py:75-81`: apagar `_user_last_msg = {}` (77) e `_consultar_estoque_active_user_id = None` (81) com seus comentários, e as duas atribuições que o C1.1 manteve no wrapper. `_atacado_users` (bot.py:72, lido em 1230) **NÃO** vira thread-local — é memória por cliente entre turnos; thread-local apagaria o contexto de atacado a cada mensagem e o card voltaria a preço de varejo.

**P1.9 — `_resolver_tamanho_alvo`** (perto de `_tamanhos_validos_na_msg`, bot.py:111). Substitui o bloco bot.py:335-343:
```python
def _resolver_tamanho_alvo(tamanho_alvo):
    """(tamanho_efetivo, corrigido: bool). Le o contexto do turno, nao global de modulo."""
    if not tamanho_alvo: return None, False
    ultimo_user = _ctx_last_msg()
    tokens_user = _tamanhos_validos_na_msg(ultimo_user) if ultimo_user else []
    if tokens_user and tamanho_alvo not in tokens_user:
        print(f"[GUARD] tamanho '{tamanho_alvo}' fora da msg do cliente {tokens_user} -> forcando '{tokens_user[0]}'")
        return tokens_user[0], True
    return tamanho_alvo, False
```
Em bot.py:335: `tamanho_alvo, _ = _resolver_tamanho_alvo(tamanho_alvo)`. **Não** alterar o retorno da tool aqui (é F2) — o `bool` fica disponível de graça.

**P1.10 — T3/T3b + concorrência real.** T3: dois buffers (`test_hard_a` = "quero baby doll G", `test_hard_b` = "tem no GG?"), `_executar_turno` stubado gravando `(uid, bot._ctx_last_msg())`, duas threads simultâneas → cada tupla tem o texto **do próprio** uid. T3b: `_set_turn_ctx("u","quero G")` → `_resolver_tamanho_alvo("GG") == ("G", True)`; `_clear_turn_ctx()` → `("GG", False)` (fora de turno o guard não inventa correção). Depois: `python tests/stress_test.py` (5 clientes, 3 turnos cada, Gemini real — rodar **uma** vez, não por commit) e conferir que nenhum `[GUARD]` força tamanho que aquele cliente não digitou.

**P1.11 — Gate do C1.2.** `run_eval.py --dimension tamanho` (cenários `tamanho-lingerie-confirma-por-categoria`, `sexshop-sem-perguntar-tamanho`) e depois a suíte inteira. Se `--dimension tamanho` falhar, o suspeito é esquecer o unpack `tamanho_alvo, _ =` (o filtro vira tupla e a RPC volta vazia).

**P1.12 — `save_message` devolve o id.** `bot.py:213-218`: `r = supabase.table("chat_history").insert({...}).execute()` e `try: return (r.data or [{}])[0].get("id") except Exception: return None`. Coluna `id` é uuid. Nenhum chamador atual usa o retorno (1331, 1417) — compatível.

**P1.13 — `get_history(user_id, limit=30, excluir_id=None)`.** `bot.py:220-262`: `.select("id, role, content, created_at")` (era sem `id`); `[AJUSTADO]` quando `excluir_id is not None`, usar `.limit(limit + 1)`; após o filtro de handoff (258) e **antes** do `rows.reverse()` (261): `if excluir_id is not None: rows = [r for r in rows if r.get("id") != excluir_id]`. Map final continua devolvendo só `{role, parts}` (contrato exigido também por `_montar_mensagem_operador`). Exclusão por **id**, não por texto — imune a cliente que repete a mesma frase.

**P1.14 — Cortar a dupla alimentação + normalizar as duas pontas do history.** bot.py:1331 e 1393, dentro de `_executar_turno`:
```python
msg_id = save_message(user_id, "user", texto_completo)      # 1331 — NAO mover (ver abaixo)
...
history = get_history(user_id, excluir_id=msg_id)           # 1393
if msg_id is None and history and history[-1]["role"] == "user" and history[-1]["parts"] == [texto_completo]:
    print("[HIST] insert sem id; removendo a linha atual por texto"); history = history[:-1]
# [AJUSTADO] CAUDA: colapsar corrida final de turnos 'user' (318 casos reais no banco)
_n = 0
while len(history) >= 2 and history[-1]["role"] == "user" and history[-2]["role"] == "user":
    anterior = history.pop(-2); _n += 1
if _n: print(f"[HIST] cauda user colapsada: {_n}")
while history and history[0]["role"] != "user":
    history.pop(0)                                          # a API exige history comecando em turno do usuario
```
`chat.send_message(texto_completo)` (1404) permanece a **única** entrega da mensagem ao modelo. **Por que remover do history e NÃO mover o `save_message`:** durante o silêncio pós-handoff a atendente humana lê a mensagem do cliente em `chat_history` (contrato declarado no comentário de bot.py:1356) e `_montar_mensagem_operador` a lê no meio do turno; mover o save para depois de `get_history` apagaria a mensagem do cliente exatamente nas 2h de handoff.

**P1.15 — Fechar o par no caminho de erro** `[AJUSTADO]`. No `except` de `_executar_turno` (bot.py:1461-1488), logo após enviar o fallback: `save_message(user_id, "model", _fallback)`.

**P1.16 — T4 + gate do C1.3.** uid `test_hard_hist`; `save_message(uid,"user","oi")`; `sleep(0.05)`; `save_message(uid,"model","ola!")`; `sleep(0.05)`; `mid = save_message(uid,"user","G")`; ASSERT `mid is not None`; `h = get_history(uid, excluir_id=mid)`; ASSERT `h == [{"role":"user","parts":["oi"]},{"role":"model","parts":["ola!"]}]`. T4b: inserir "oi"/"model"/"user A"/"user B" e conferir que a cauda colapsada deixa **um** turno `user` final. Depois: eval completa vs baseline.

**P1.17 — `/health`.** `bot.py:2028-2035`, no dict `body`: `"buffers_pendentes": len(message_buffers)` e `"turnos_conhecidos_desde_boot": len(_user_turn_locks)  # contador cumulativo, NAO alarme` `[AJUSTADO]`. **Não** alterar a regra de `healthy` (continua idade do sync).

### Commits F1
1. `fix(turno): serializa turno por cliente e drena buffer no inicio — fim do reenvio de cards` (P1.2-P1.7: ctx de thread + lock de turno + `_executar_turno` + wrapper com try/except + Timer daemon + T0/T1/T2)
2. `fix(turno): contexto do turno em thread-local — fim do cross-talk entre clientes` (P1.8-P1.11)
3. `fix(ia): mensagem do cliente entregue UMA vez ao Gemini` (P1.12-P1.16)
4. `chore(health): buffers_pendentes no /health` (P1.17)

### Como PROVAR
- T0 falha no código de hoje e passa depois → a corrida existe e morreu.
- T1 `["oi","tem mais?"]`; T2 buffer não órfão; T3 sem cross-talk; T4 sem dupla alimentação.
- Log em produção (pós-religar): `[TURNO] <user>: espera_lock=NNNms` com NNN>0 = serialização atuando; `buffer vazio (ja coalescido...)` = coalescing.
- `curl /health` → `buffers_pendentes = 0` em repouso. Valor >0 persistente em repouso = entrada órfã, o defeito voltou.
- **SQL de fechamento `[AJUSTADO]`** (filtro de data **obrigatório**, ver limite abaixo):
```sql
with u as (select user_id, created_at, content,
                  lag(content) over (partition by user_id order by created_at) prev,
                  extract(epoch from created_at - lag(created_at) over (partition by user_id order by created_at)) gap
           from chat_history where role='user' and created_at > '<data do deploy>')
select count(*) filter (where gap <= 120) pares_corrida,
       count(*) filter (where gap > 120)  pares_gap_longo,
       count(distinct user_id) usuarios
from u where prev is not null and content <> prev and position(prev in content) = 1;
```
**Critério: `pares_corrida = 0`.** *Não* exigir zero total: cliente que estende a própria frase ("oi" → "oi tudo bem") casa o mesmo predicado. Baseline medida na revisão: **301 pares / 72 usuários; 133 com gap ≤120s (corrida); 168 acima (vazamento), mediana 247s, máximo 767.965s = 8,9 dias; 318 linhas user-após-user.** Use **esta** query, senão antes e depois medem coisas diferentes.
- **Limite declarado da prova:** os 67 usuários já envenenados continuam recebendo `get_history(limit=30)` com rows de dias de texto concatenado quando voltarem a falar. Limpeza é frente própria (backlog B7).

---

## 3. FRENTE 2 — VERDADE NO RETORNO DAS TOOLS `CÓDIGO` + `DDL`  — **VERSÃO CORRIGIDA (revisor reprovou)**

### Correções aplicadas
- `[AJUSTADO — AJUSTE PRINCIPAL]` O texto do `aviso` foi reescrito. O original era beco sem saída **e** nova mentira: (a) mandava "chame a ferramenta de novo com `tamanho_llm`", mas o guard lê a mensagem do cliente, que **não muda dentro do turno** → a re-chamada é corrigida outra vez e o modelo entra em loop; (b) afirmava "todos os produtos abaixo são do tamanho X", falso depois do overlap (`G/GG` para `G`, `ÚNICO` para `UNICO`) — o campo criado para matar evidência falsa emitiria evidência falsa.
- `[AJUSTADO]` `tokens_user` é calculado **sempre**, fora do `if tamanho_alvo`, e a tabela ganha `llm_omitiu_tamanho` — o modo de falha mais provável do P3 (o LLM **omite** o tamanho que o cliente deu) era invisível.
- `[AJUSTADO]` `_log_filtro_evento` também nos **dois returns de erro** (bot.py:348 e 424), e `id_loja_alvo` hoistado para antes do `try` — sem isso `count(*)` não é "chamadas da tool" e todo denominador de métrica está errado.
- `[AJUSTADO]` `tool_filtro_eventos` entra na tupla de cleanup de `run_eval.py:721` **no mesmo commit** que cria a tabela. Com o bot desligado, a suíte seria 100% do volume e a métrica nasceria inválida.
- `[AJUSTADO]` Dois **checkTypes novos** no harness — sem eles os 3 cenários da dimensão `tamanho` são passes triviais (o vocabulário atual, run_eval.py:431-444, não expressa "a tool não voltou vazia" nem "a Luna não negou estoque").
- `[AJUSTADO]` `tests/test_tamanho_tokens.py` no estilo do repo (helper `ok()` como em `tests/test_cloud_migration.py`, `if __name__ == "__main__"`, contador de falhas, `sys.exit(1)`) e com os 35 pares `(tamanho, tamanho_tokens)` **congelados como fixture** — o teste no estilo pytest rodado como script executaria zero asserções e sairia 0.
- `[AJUSTADO]` `NOTIFY pgrst, 'reload schema';` no fim da 0011 e do seu rollback.
- `[AJUSTADO]` Precondição "ninguém fora do repo chama a RPC com 3 chaves" **continua aberta** e é gate do DROP: `pg_stat_statements` desta instância não tem `stats_since`, então as 16 chamadas registradas são **indatáveis**, e "observar erros após o drop" não vale nada com o bot desligado.
- `[DECISÃO nova]` Subir a truncagem de `serializar_tool_calls` (bot.py:1137, corte em 2000 chars) para **4000**. O revisor mostrou que pôr `filtro_aplicado`+`aviso` antes de `produtos` come o orçamento do digest de que `_prod_ids_from_responses` (run_eval.py:205) depende → a substituição `<PRODn>` degradaria silenciosamente. É log em `bot_turns`, não contexto do Gemini: o custo é armazenamento, o ganho é o harness intacto.
- `[AJUSTADO — ORDEM]` Commits 1→2→3 são **encadeados** (2 e 3 consomem `_tokens_tamanho`/`tokens_alvo`/`dropados` do 1). O rollback só na ordem inversa. Só os commits 4 (drop) e 5 (cenários) são independentes. Os passos 5-7 e 9 ficam **bloqueados pela F1** (o `aviso` cita a mensagem do cliente; com globais de módulo isso injetaria a mensagem de OUTRO cliente).

### Passos
**P2.1 — Baseline** (ou reaproveitar o CSV pós-F1, que é o estado real).

**P2.2 — `_tokens_tamanho`, espelho exato de `normalize_tamanho_tokens`.** `bot.py:140` (após `_tamanhos_validos_na_msg`): as duas strings de translate copiadas **literalmente** do `prosrc` da função SQL, `_TRANS_TAMANHO = str.maketrans(...)`, `_SEP_TAMANHO = re.compile(r"[\s\-/(),|]+")`, e `_tokens_tamanho(txt)` = `translate` **antes** de `upper` (igual ao SQL) + split + descarte de vazios. **Não** usar `unicodedata`/NFKD.

**P2.3 — `tests/test_tamanho_tokens.py`** (estilo do repo). Fixture congelada dos 35 valores distintos de `tamanho` e seus `tamanho_tokens` (0 linhas com token stale hoje → fixture fiel) **sempre** asserida; banco só como camada extra quando houver `.env`. Casos obrigatórios: `'ÚNICO'→['UNICO']`, `'P/M'→['P','M']`, `'G/GG'→['G','GG']`, `'38-40'→['38','40']`, `'TAM 42'→['TAM','42']`, `''→[]`. Ao rodar SQL com acento pelo Git Bash o payload do curl mangla `Ú` — usar cliente Python ou `chr(218)`.

**P2.4 — Overlap no lugar da igualdade exata.** `bot.py:426-440`. Antes da RPC: `tokens_alvo = set(_tokens_tamanho(tamanho_alvo))`. Trocar `if p['tamanho'].upper() == tamanho_alvo:` (433) por manter a linha se `not tamanho_alvo or not tokens_alvo or (tokens_alvo & set(_tokens_tamanho(p.get('tamanho'))))`. `not tokens_alvo` espelha o branch `cardinality(...)=0` da RPC (0009:54). `p.get('tamanho')` corrige de graça um `AttributeError` latente (hoje `p['tamanho'].upper()` está **fora** do try). Acumular `dropados` (tamanhos crus descartados) e `n_dropados_igualdade_legado` (linhas que sobreviveram ao overlap mas morreriam na igualdade). **A RPC já faz overlap** (`tamanho_tokens && tokens_alvo`, 0009:55) — o culpado era só o Python; e o dano não são 38 linhas, são **623**: `'ÚNICO'.upper() != 'UNICO'` derrubava 92,4% (623/674) do que a RPC aprovava. `G ≠ GG` é preservado (conjuntos disjuntos), então a expansão que o prompt proíbe **não** volta.

**P2.5 — Estado do guard + hoist.** `bot.py:330-343`: após `tamanho_alvo = ...`, `tamanho_llm = tamanho_alvo`, `guard_acionou = False`, `tokens_user_log = _tamanhos_validos_na_msg(_ctx_last_msg() or "")` **sempre** `[AJUSTADO]`, `llm_omitiu_tamanho = bool(tokens_user_log) and not tamanho_alvo`. Hoistar `id_loja_alvo` (hoje atribuído em 352, dentro do try, depois do early-return de embedding em 348) para **antes** do try `[AJUSTADO]`.

**P2.6 — `filtro_aplicado` + `aviso` (texto novo).** bot.py:442-457:
```python
filtro_aplicado = {"tamanho": tamanho_alvo, "tamanho_tokens": sorted(tokens_alvo),
                   "id_loja": id_loja_alvo,
                   "observacao_tamanho": ("Cada item traz seu proprio campo 'tamanho'. Ele pode ser COMPOSTO "
                     "('P/M','G/GG') ou acentuado ('UNICO'/'ÚNICO') e ainda assim atender o tamanho pedido. "
                     "Nunca afirme que um item e do tamanho X: leia o campo 'tamanho' do item.")}
# aviso SO existe quando guard_acionou; nunca mandar None ao Gemini
aviso = (f"INSTRUCAO INTERNA (nao repita ao cliente): a ferramenta foi chamada com tamanho '{tamanho_llm}', "
         f"mas nesta mensagem o cliente escreveu {tokens_user_log}. A busca foi executada LITERALMENTE em "
         f"'{tamanho_alvo}'. Nao afirme nada sobre '{tamanho_llm}'. A ferramenta so aceita o tamanho que o "
         f"cliente escreveu nesta mensagem — se precisar de outro tamanho, PERGUNTE ao cliente.")
return {"status": "sucesso", "filtro_aplicado": filtro_aplicado, **({"aviso": aviso} if guard_acionou else {}), "produtos": selecao}
```
Caminho **vazio** (440) fica simétrico: `{"status":"vazio","filtro_aplicado":...,[aviso],"msg": f"Nenhum produto com estoque para o termo buscado no tamanho {tamanho_alvo} na loja {id_loja_alvo}. O filtro foi aplicado literalmente; nao conclua nada sobre outro tamanho sem chamar a ferramenta de novo."}`. `extrair_produtos_de_tool_results` (bot.py:1080) só lê `payload.get('produtos')` → chaves de topo novas são ignoradas; **a forma de cada item de `produtos` não muda** (restrição dura). Atualizar **só a seção `Returns:`** da docstring de `consultar_estoque_supabase` (bot.py:312-326) — a docstring **é** a function declaration entregue ao Gemini, logo é correção de CÓDIGO e não exige deploy de prompt.

**P2.7 — Truncagem do digest 2000→4000** `[DECISÃO]`. `bot.py:1137-1155`.

**P2.8 — `DDL` migration 0010** (`migrations/0010_tool_filtro_eventos.sql` + rollback):
```sql
CREATE TABLE IF NOT EXISTS public.tool_filtro_eventos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  user_id text, tool text NOT NULL DEFAULT 'consultar_estoque_supabase',
  termo_cliente text, tamanho_llm text, tamanho_user_tokens text[],
  tamanho_aplicado text, tamanho_aplicado_tokens text[],
  guard_acionou boolean NOT NULL DEFAULT false,
  llm_omitiu_tamanho boolean NOT NULL DEFAULT false,     -- [AJUSTADO]
  id_loja_aplicado text, n_candidatos integer, n_validados integer,
  n_dropados_overlap integer, n_dropados_igualdade_legado integer,
  tamanhos_dropados text[], status_retornado text );
CREATE INDEX IF NOT EXISTS idx_tfe_created_at ON public.tool_filtro_eventos (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tfe_guard ON public.tool_filtro_eventos (guard_acionou) WHERE guard_acionou;
ALTER TABLE public.tool_filtro_eventos DISABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON public.tool_filtro_eventos TO anon, authenticated, service_role;
NOTIFY pgrst, 'reload schema';
```
RLS OFF + grant a `anon` **não é descuido**: o bot usa a chave anon (JWT role=anon) e todas as tabelas atuais (`bot_turns`, `card_envios`, `chat_history`, `conversation_handoffs`) seguem esse padrão. RLS sem policy faria o insert falhar dentro do `try/except` e a observabilidade sumiria em silêncio. Rollback: `DROP TABLE IF EXISTS public.tool_filtro_eventos;`

**P2.9 — `_log_filtro_evento`** junto de `log_turn` (bot.py:276), no mesmo estilo defensivo (try/except que só imprime, nunca propaga). `user_id = _ctx_user_id()` `[AJUSTADO — nunca o global de módulo]`. Chamado **antes de todos os 4 returns** (sucesso, vazio, erro em 348, erro em 424) com `status_retornado` correspondente. É append-only e **duplica** no retry de 3 tentativas (5e4f727) — esperado para log; consultas de métrica agrupam por turno, não somam cru.

**P2.10 — Cleanup no harness** `[AJUSTADO]`. `tests/eval/run_eval.py:721`: `for tbl in ("bot_turns","chat_history","conversation_handoffs","tool_filtro_eventos"):`.

**P2.11 — checkTypes novos** `[AJUSTADO]`. Em `run_eval.py`:
- determinístico `check_tool_nao_retornou_vazio(tc, params)`: varre `tc['responses']` das SEARCH_TOOLS procurando `'"status": "vazio"'` no `result_digest` (a chave `status` continua a primeira do dict → sobrevive à truncagem) e falha se houve busca **com tamanho** e todas voltaram vazias.
- juiz `check_nao_nega_estoque_sem_base(tc, params)`: rubrica = *a Luna não pode (1) afirmar que não tem no tamanho pedido quando a tool retornou produtos, nem (2) afirmar qualquer coisa sobre um tamanho diferente do que consta em `filtro_aplicado`, nem (3) dizer que um item é do tamanho X quando o campo `tamanho` do item é composto.*
Registrar em `CHECKS` (431-444), `SEVERITY` (452-465: (1) grave, (2) grave) e o segundo em `JUDGE_CHECKS` (446-449).

**P2.12 — 3 cenários** em `scenarios.json`, dimension `tamanho` (lembrar: `params` é **string JSON**): `tamanho-unico-acento-nao-da-falso-vazio` (o caso dos 623), `tamanho-composto-pm-aparece-para-m`, `tamanho-guard-corrige-sem-mentir`. Cada um usando os checks novos + `sent_at_least_one_message`. Preferir asserção sobre **ausência de negação de estoque** a asserção sobre `id_produto` específico (o ERP zera itens).

**P2.13 — `DDL` migration 0011: dropar a sobrecarga legada.** **BLOQUEADO até fechar a precondição** `[AJUSTADO]`: listar as edge functions do projeto e fazer grep em n8n/painel/planilha por chamada com 3 chaves; se não fechar por evidência positiva, **instrumentar** (substituir o corpo da 3-args por plpgsql que registra a chamada) e observar N dias antes do drop.
```sql
DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(vector, double precision, integer);
NOTIFY pgrst, 'reload schema';
```
Rollback com o corpo exato capturado por `pg_get_functiondef(252201)` + `NOTIFY`. Confirmado: 0 dependentes em `pg_depend`; único caller no repo é bot.py:353-360 e sempre envia 5 chaves; a legada **é alcançável** (16 chamadas em `pg_stat_statements`) e, como a de 5 args tem DEFAULT nos 2 últimos, uma chamada com 3 chaves casa com **as duas** assinaturas → 300 Multiple Choices ou resolução na errada (sem filtro de loja, com estoque zero).

### Commits F2 (ordem obrigatória 1→2→3)
1. `fix(tools): re-filtro de tamanho por overlap de tokens — fim do falso 'nao tenho'` (P2.2-P2.4)
2. `feat(tools): retorno declara filtro_aplicado + aviso quando o guard corrige` (P2.5-P2.7)
3. `feat(obs): tool_filtro_eventos + cleanup no harness` (P2.8-P2.10, migration 0010)
4. `chore(db): dropa sobrecarga legada de 3 args` (P2.13, migration 0011) — independente
5. `test(eval): checks e cenarios de tamanho` (P2.11-P2.12) — independente

### Como PROVAR
- `python tests/test_tamanho_tokens.py` verde (fixture + banco).
- **Repro dirigido do acento**, no REPL: `consultar_estoque_supabase("<acessorio>", "UNICO", "244033")` → **antes** `{'status':'vazio'}`, **depois** `sucesso` com itens cujo `tamanho` é `'ÚNICO'`.
- SQL que quantifica o ganho: `with alvos as (select unnest(array['P','M','G','GG','UNICO']) alvo) select a.alvo, count(*) filter (where p.tamanho_tokens @> array[a.alvo]) rpc_aprova, count(*) filter (where p.tamanho_tokens @> array[a.alvo] and upper(p.tamanho) <> a.alvo) python_derruba from alvos a join produtos_estoque p on p.estoque>0 and p.tamanho_tokens && array[a.alvo] group by 1;` — hoje **UNICO 674/623, P 618/20, M 890/20, GG 789/18, G 829/18**.
- Os 2 checks novos **falham** no código antigo e passam no novo (rodar `--dimension tamanho` nas duas HEADs).
- `select count(*) from pg_proc where proname='buscar_produtos_semantico'` = 1, e `pg_get_function_identity_arguments` = a de 5 args (até a F5). Smoke pós-drop: uma busca real devolvendo 200 com linhas.
- Métrica (pós-religar): `n_dropados_igualdade_legado > 0` em qualquer linha de `tool_filtro_eventos` **com `user_id not like 'eval_%'`** é um falso *"não tenho nesse tamanho"* que o código antigo teria produzido. Calibrar expectativa: `bot_turns` tem só 46 turnos e os tamanhos registrados são `null` (13), `'G'` (9), `'GG'` (7) — o defeito do tamanho único é real mas **latente**.

---

## 4. FRENTE 3 — FOTOS SOB DEMANDA `CÓDIGO`  — **VERSÃO CORRIGIDA (revisor reprovou)**

### Correções aplicadas
- `[AJUSTADO — AJUSTE PRINCIPAL]` **Chave `pid` normalizada dentro dos helpers** (`pid = str(int(float(pid)))`) e **uma única função pura** `_fotos_novas()` alimentando tool e sender. O plano original gravava com `int` e lia com `str` → `vistas` sempre vazia na tool: produto de 1 foto (216 no catálogo, 198 com estoque) prometia 1 e enviava **0**; de 2 fotos (149) prometia 2 e enviava 1. Era o P4 reproduzido no maior bucket do catálogo.
- `[AJUSTADO]` `criar_tool_mostrar_fotos(user_id, pendentes)` — **closure**, no molde de `criar_tool_transferir` (bot.py:946, instanciada em 1368). Elimina `_turn_local`, elimina leitura de global e remove a dependência do detalhe interno da F1.
- `[AJUSTADO]` **Um produto por turno**: `FOTOS_MAX_POR_PRODUTO_TURNO == FOTOS_MAX_POR_TURNO == 4`; o 2º pid devolve `limite_turno`. O par 4/6 do plano original fazia a tool prometer 4+4 e o sender entregar 4+2.
- `[DECISÃO — divergindo do revisor]` As extras continuam sendo enviadas **dentro** do `turn_lock`, logo após `_executar_turno` retornar. O revisor pediu mover para depois do cleanup do buffer, mas a premissa dele era o código de **hoje** (pop no fim). Com a F1 o pop acontece no **início** do turno: mensagem que chegar durante os ~4s de sleep entra num buffer **novo** e o turno seguinte espera no lock — não há mensagem perdida nem janela ampliada. Enviar fora do lock reintroduziria intercalação de mídia entre turnos.
- `[AJUSTADO]` Check determinístico é `extra_photos_sent` (conta **só** as extras, separadas por legenda), não `photos_sent_unique` sobre o `media_sent` acumulado do cenário (run_eval.py:571-572) — o re-envio do card de um produto re-recomendado no turno 2 é legítimo e geraria fail grave falso; e `min:2` era satisfeito pelos cards do turno 1, passando com **zero** extras.
- `[AJUSTADO]` Cenário 1 usa **id literal 8147660** (5 fotos, estoque P/M/G), não `<PROD1>` (que resolve para o 1º id da RPC, podendo ser produto de 1 foto ou sem foto → cenário não testaria nada).
- `[AJUSTADO]` `_n_fotos` no harness **sem** o `.limit(5)` herdado de `_tem_foto` (run_eval.py:159-179) — os 19 produtos com 6-10 fotos chegariam ao juiz com fato falso.
- `[AJUSTADO]` Premissa "foto de menor `id` = principal do ERP" validada em **10 produtos amostrados** antes do commit 1 (o plano validou em 1). Nenhum check olha *qual* URL foi enviada, então a piora seria silenciosa.
- `[AJUSTADO]` Declarado: **esta frente NÃO fecha o P4.** Sem o bloco de prompt (F6) o acionamento depende só da docstring. Log `[fotos] tool_chamada=0/1` mede a taxa.
- `[AJUSTADO]` Legenda das extras com o marcador **acentuado** `_cód: {pid}_`, igual ao card (bot.py:1248), para `card_envios.legenda` e o reply-to-card ficarem consistentes.
- `[AJUSTADO]` `prompt_frente3_fotos.md` registra o pré-requisito operacional `FOTOS_SOB_DEMANDA=1` e a correção da **linha 295** do prompt (`"um dos 4 acima"` → 5), que o plano original esqueceu (só tratou 285 e 303).

### Passos
**P3.1 — Baseline** + `pip install -r requirements.txt` (Gate 0.4) + **amostrar 10 produtos com 2+ fotos** e olhar a foto de menor `id` (premissa da ordem).

**P3.2 — Base.** `bot.py:86`: `FOTOS_SOB_DEMANDA = (os.getenv("FOTOS_SOB_DEMANDA","1") != "0")` (kill switch), `FOTOS_MAX_POR_PRODUTO_TURNO = int(os.getenv("FOTOS_MAX_PROD","4"))`, `FOTOS_MAX_POR_TURNO = FOTOS_MAX_POR_PRODUTO_TURNO`, `FOTOS_SLEEP_SEG = 0.8`, `FOTOS_TTL_SEG = 6*3600`, `FOTOS_MAX_USERS_REGISTRY = 500`, `_fotos_vistas = {}`, `_fotos_vistas_lock = threading.Lock()`. Helpers (bot.py:310), **todos** normalizando `pid = str(int(float(pid)))` na primeira linha `[AJUSTADO]`:
- `_fotos_do_produto(pid) -> list[str]`: `.select("id, imagem_url, imagem_mini_url").eq("produto_id", pid).order("id", desc=False)`; por row usa `imagem_url or imagem_mini_url` (**nunca as duas** — `mini_` é a MESMA foto em baixa resolução: 843/843 rows com mini ≠ full, nenhum mini nulo); dedupe preservando ordem; `[]` em exceção. Docstring registra: `id ASC` = ordem do ERP porque `sync_images.py:122-132` faz **um** insert em lote na ordem do array `fotos`; e que o **valor** do id **nunca** pode ser persistido porque `sync_images.py:119` faz DELETE+INSERT a cada sync.
- `_fotos_ja_vistas(user_id, pid) -> list[str]` (aplica TTL) · `_marcar_fotos_vistas(user_id, pid, urls)` (trim LRU acima de `FOTOS_MAX_USERS_REGISTRY`) · **`_fotos_novas(user_id, pid) -> (fotos, vistas, novas)`** = fonte única usada pela tool **e** pelo sender `[AJUSTADO]`.
- Cuidado de tipo: `produtos_imagens.produto_id` é BIGINT e `produtos_estoque.id_produto` é TEXT; via PostgREST passar **string** funciona (bot.py:406 já faz) — nunca escrever SQL cru juntando as duas sem `::text`.

**P3.3 — Foto principal determinística + `n_fotos`.** `bot.py:398-418`: acrescentar `.order("id", desc=False)` na query de imagens e trocar `mapa_imagens` (pid→url) por `mapa_fotos` (pid→list). Em 415-416: `fotos = mapa_fotos.get(str(p['id_produto'])) or []`; `p['imagem'] = fotos[0] if fotos else None`; `p['n_fotos'] = len(fotos)`. Em bot.py:453-454, **no loop de `selecao`** (não dentro do try): `p['n_fotos'] = int(p.get('n_fotos') or 0)` antes de `p['tem_foto']`. **Não** colocar a lista de URLs no payload (custo de token e o §0 do prompt proíbe URL) — as URLs são relidas frescas no envio.

**P3.4 — `consultar_produto_por_id` deixa de ser cega.** `bot.py:461-519`: após montar `variacoes`, `fotos = _fotos_do_produto(id_produto_normalizado)` e acrescentar ao dict de retorno `"imagem"`, `"n_fotos"`, `"tem_foto"`. Atualizar a linha `Returns:` da docstring. Uma query extra indexada (`idx_produtos_imagens_produto_id`), ≤10 rows.

**P3.5 — Cache para de gravar `imagem: None`.** `bot.py:1130`: `"imagem": produto.get('imagem'),`. Efeito: produto revalidado por id volta a sair como card **com** foto.

**P3.6 — Registrar a foto do card como vista.** `bot.py:1273-1275` (dentro de `renderizar_mensagem_estruturada`), após `wamid = enviar_midia_whatsapp(...)`: `_marcar_fotos_vistas(user_id, pid, [url])`. Não mudar `_legenda_card` (bot.py:1235) — a legenda alimenta `card_envios` e o reply-to-card.

**P3.7 — `criar_tool_mostrar_fotos(user_id, pendentes)`** (bot.py:520). Read-only: normaliza o id com o mesmo cast de bot.py:476-481 (o Gemini manda `54557957.0`); `fotos, vistas, novas = _fotos_novas(user_id, pid)`; `sem_foto` se `not fotos`; `sem_novas` se `not novas`; `limite_turno` se `pendentes and pid not in pendentes`; senão `pendentes.append(pid)` (dedupe) e retorna `{"status":"ok","id_produto":pid,"nome":...,"n_fotos":len(fotos),"vai_enviar":min(len(novas),FOTOS_MAX_POR_PRODUTO_TURNO),"ja_vistas":len(vistas),"restantes":max(0,len(novas)-FOTOS_MAX_POR_PRODUTO_TURNO)}`. **Não envia e não escreve em `_fotos_vistas`** → o retry de 3 tentativas (bot.py:1401-1412) é inofensivo por construção. Docstring model-facing: quando usar; que o sistema envia **depois** da resposta; que **nunca** escreva URL; e o significado de cada status — `ok` = anuncie de forma neutra, **nunca** diga qual ângulo nem afirme cor; `sem_foto` = seja transparente; `sem_novas` = o cliente já recebeu TODAS e **só nesse caso** ofereça atendente; `restantes>0` = se o cliente pedir, chame de novo no próximo turno.

**P3.8 — Sender.** `_enviar_fotos_extras_pendentes(user_id, pendentes)`: para cada pid, `_fotos_novas` fresco, corta em `min(FOTOS_MAX_POR_PRODUTO_TURNO, budget)`; legenda da 1ª foto escolhida **por dado**: `len(fotos) <= 2` → `f"Aqui o outro angulo que eu tenho dessa peca 💕\n_cód: {pid}_"`, `>= 3` → `f"Aqui as outras fotos que eu tenho dessa peca 💕\n_cód: {pid}_"`; demais fotos só `f"_cód: {pid}_"` (nunca legenda vazia — o payload cloud sempre manda `caption`). Por URL: `wamid = enviar_midia_whatsapp(...)`; `if WHATSAPP_PROVIDER=="cloud" and isinstance(wamid, str): registrar_card_enviado(...)` (o `isinstance(str)` é obrigatório: o stub do eval devolve dict); `_marcar_fotos_vistas(...)`; `sleep`. `finally: pendentes.clear()`. Log: `[fotos] tool_chamada={1 if pendentes_iniciais else 0} extras_enviadas={n}`. **Wiring:** `_executar_turno(user_id, texto_completo, fotos_pendentes)` cria a tool com a closure na lista de `tools` (bot.py:1372-1384, condicional a `FOTOS_SOB_DEMANDA`); o wrapper (P1.4) chama, **depois** de `_executar_turno` e **só se `turno_ok`**:
```python
        if turno_ok and FOTOS_SOB_DEMANDA and fotos_pendentes:
            try: _enviar_fotos_extras_pendentes(user_id, fotos_pendentes)
            except Exception as e: print(f"[fotos] envio extra falhou: {e}")
```
**Deliberadamente NÃO** adicionar `mostrar_fotos_produto` à tupla de bot.py:1094 (`extrair_produtos_de_tool_results`): se o pid entrar no cache e o modelo o listar em `produtos_recomendados`, o renderizador reenvia o **card** com a foto principal = foto duplicada.

**P3.9 — Kill switch no registro da tool** (bot.py:1372-1384). Com `FOTOS_SOB_DEMANDA=0` a tool desaparece da function declaration e o sender vira no-op → rollback sem redeploy.

**P3.10 — `t_fotos.py`** no scratchpad, stubs de envio instalados **antes** de qualquer chamada, `FOTOS_SLEEP_SEG=0`. Asserts: **(A) ordem** — `_fotos_do_produto("8060563")` len 10 igual ao `order by id`; `"63252641"` len 1; `"8147660"` len 5. **(B) idempotência** — duas chamadas da tool no mesmo turno → `pendentes` com 1 elemento, sender envia 4 URLs distintas, 2ª chamada do sender envia 0. **(C) paginação** — turno novo → `vai_enviar 1`, envia a 5ª; 3º turno → `sem_novas`, `n_fotos 5`. **(D) sem duplicar a principal** — render marca a foto do card, depois extras → 5 mídias, 5 URLs distintas. **(E) 1 foto** — `mostrar_fotos_produto(63252641)` num user novo → `ok`, `n_fotos 1`, sender envia **1**; 2º turno → `sem_novas`. **(F) `[AJUSTADO]`** — após o card, `mostrar_fotos_produto` do MESMO produto devolve `ja_vistas: 1` (com a chave inconsistente do plano original devolveria 0). **(G)** 2 pids no mesmo turno → o 2º devolve `limite_turno`.

**P3.11 — Harness.** `extra_photos_sent(tc, params)`: separa `tc['media_sent']` por legenda (card contém `R$`/`*`; extra = anúncio neutro ou `_cód: N_` puro) e exige (a) nenhuma URL repetida **entre as extras**, (b) `>= params['min']` extras, (c) nenhuma URL de extra igual à de um card do mesmo cenário. `photo_language_neutral` (juiz): fatos = nº de mídias + `n_fotos` real via `_n_fotos(pid)` **sem `.limit(5)`**; rubrica = não pode dizer qual lado/ângulo, não pode afirmar cor, não pode oferecer atendente para *tirar* foto quando ainda há foto para mandar. Registrar em `CHECKS`, `JUDGE_CHECKS`, `SEVERITY` (`extra_photos_sent` grave, `photo_language_neutral` média). Cenários: `foto-mais-fotos-sob-demanda` (id literal 8147660, 4 turnos: pedir camisola M → "manda mais fotos do cod 8147660" → "quero ver mais" (5ª foto) → "quero mais ainda" (`sem_novas` sem promessa), `extra_photos_sent {"min": 3}`) e `foto-unica-esgotou-angulos` (63252641, 1 foto; **não** assertar handoff — o motivo `pedido_foto` só existe após a F6).

**P3.12 — Gate.** `run_eval.py --dimension foto` para iterar, suíte completa após cada commit de código, diff contra o baseline.

**P3.13 — Smoke (pós-religar, Gate 0.5).** `FOTOS_SOB_DEMANDA=1`, replicas=1 confirmado. Roteiro: pedir camisola M → **responder** (botão Responder) o card de um produto com 5+ fotos com "manda mais fotos" (o reply-to-card injeta `id_produto`, bot.py:1918-1925 + `_lookup_card` 1818-1829) → esperado: texto primeiro, depois 4 imagens **novas**; "quero ver mais" → a 5ª; "quero mais ainda" → "são todas que eu tenho". Conferir `[fotos] extras_enviadas=N` no log e `select wamid, id_produto, legenda, created_at from card_envios order by created_at desc limit 10;` (1 row por card + 1 por extra, id_produto correto).

**P3.14 — `prompt_frente3_fotos.md`** (staged, **sem** deploy): substituição do parágrafo `FOTOS:` (§5), adição em §2 passo 5, §15 virando 5 motivos (**linhas 285, 295 e 303**) com `pedido_foto`, §0 proibindo URL. Registrar: `FOTOS_SOB_DEMANDA=1` é pré-requisito do deploy (com o switch em 0 o prompt mandaria chamar tool inexistente). Não há constraint em `conversation_handoffs.motivo` → motivo novo não exige DDL.

### Commits F3
1. `feat(fotos): ordem deterministica + n_fotos nas duas tools de produto` (P3.2-P3.6)
2. `feat(fotos): mostrar_fotos_produto (closure) + envio pos-turno idempotente + kill switch` (P3.7-P3.9)
3. `test(eval): extra_photos_sent, photo_language_neutral e 2 cenarios` (P3.11)
4. `docs(prompt): bloco de fotos staged para o deploy unico` (P3.14)

### Como PROVAR
- Distribuição (define o teto): 1 foto→216/198 com estoque, 2→149/142, 3→24/22, 4→15/14, 5→13/13, 6→11/10, 7→3/2, 8→2/2, 9→1/1, 10→2/2. Teto 4 cobre **integralmente 191 dos 208** produtos com 2+ fotos e estoque; os 17 restantes paginam.
- Dedupe: `select produto_id, imagem_url, count(*) from produtos_imagens group by 1,2 having count(*)>1;` = 0 rows hoje (843 rows, 843 URLs distintas). O dedupe do helper é defesa: não há UNIQUE e o sync faz DELETE+INSERT.
- `t_fotos.py` A-G verdes, com destaque em (F) `ja_vistas: 1`.
- `extra_photos_sent` pass no cenário 1; eval sem regressão nos 18.
- Idempotência sob retry (pós-religar): turno cujo `bot_turns.tool_calls` mostre `mostrar_fotos_produto` mais de uma vez tem de entregar exatamente `vai_enviar` imagens.

---

## 5. FRENTE 4 — SYNC + EMBEDDING `CÓDIGO` + `DDL` + `DADO`

### Correções aplicadas
- `[AJUSTADO — AJUSTE PRINCIPAL]` A prova "o sync não apaga mais vetor" era **vácua**. O envenenamento exige um **portador** (registro que carrega a chave `embedding`) no mesmo lote de página que a vítima; `estoque_mudou > 0` só prova que havia vítimas. Agora há **controle positivo forçado**, rodado uma vez **antes** do fix (tem de apagar) e uma vez depois (tem de preservar), mais `embeds_novos > 0` como segunda condição de não-vacuidade.
- `[AJUSTADO]` `.order("id_unico")` em `carregar_estado_atual_do_banco()` (sync_erp.py:163) no **mesmo commit** do lote homogêneo. Era a pior das três paginações instáveis: linha pulada é vista como registro novo (330), leva `get_embedding` e entra no lote **com** a chave `embedding` — ou seja, **fabrica** o portador.
- `[AJUSTADO]` Cap do re-embed conta **chamadas de API (cache miss)**, não linhas. 979 linhas nulas com estoque = **293 textos distintos**; com cap de 150 linhas a cura levaria 7 ciclos (~70 min) e o plano instruiria o executor a "reposicionar o ramo" (mexer em código correto). Com `SYNC_REEMBED_MAX=300` cura em **1 ciclo**, ~6,3 min extras (293×1,3s), dentro do `misfire_grace_time=600`.
- `[AJUSTADO]` O guard de zeragem **alerta de verdade**: `_verificar_sync_e_alertar` (bot.py:2067-2093) lê **somente** `_sync_age_min()`, e `healthy` é idade pura — `LAST_RUN_OK = ciclo_ok` sozinho não dispara nada, e com o cron dedicado (`restartPolicyType: NEVER`) as globais morrem com o processo.
- `[AJUSTADO]` `regenerar_embeddings.py` dedupa e escreve por **(id_produto, nome)**: 24 id_produto têm mais de um `nome` distinto (280 linhas, 187 com estoque). Deduplicar só por id_produto estamparia o vetor de um nome arbitrário nas linhas de outro nome — a **mesma** classe de defeito que o passo diz matar. Custo: 24 embeddings extras.
- `[AJUSTADO]` `REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx;` + `ANALYZE produtos_estoque;` depois do re-embed total. HNSW no Postgres não recupera entradas mortas; reescrever ~6.739 vetores + inserir ~979 deixaria o grafo com ~metade das entradas mortas, degradando recall/latência **no caminho que a RPC usa hoje**.
- `[AJUSTADO]` Check de **recall** (aproximado vs exato) no spot-check, senão um assert que falhe será atribuído ao texto canônico e não ao índice.
- `[AJUSTADO]` Ambiente do teste de biblioteca resolvido: tabela `_test_emb_upsert` criada **uma vez** pela management API (anon não tem CREATE no `public` e supabase-py não executa SQL cru), **zero DDL** no teste, `sys.path.insert(0, ROOT)`, e venv local com os pins lidos do log de build do nixpacks.
- `[NOTA]` **Interação com a F5:** a F5 força `enable_indexscan=off` e a RPC deixa de usar o HNSW. Mesmo assim o REINDEX da F4 é obrigatório, porque entre F4 e F5 a RPC em produção é a de 0009, que **usa** o índice.

### Passos
**P4.1 — Pinar** `requirements.txt:5` com as versões de `supabase`/`postgrest`/`httpx` lidas no log de build (Gate 0.3).

**P4.2 — Retrato + sonda.** Baseline (guardar **fora** do banco):
```sql
select now() t, count(*) total, count(*) filter (where embedding is null) nulos,
       count(*) filter (where embedding is null and estoque>0) nulos_com_estoque,
       count(distinct id_produto) filter (where embedding is null) prods_com_nulo,
       count(*) filter (where estoque>0 and upper(coalesce(nome_grupo,''))='PRODUTOS MAIS VENDIDOS') mv_total,
       count(*) filter (where estoque>0 and embedding is null and upper(coalesce(nome_grupo,''))='PRODUTOS MAIS VENDIDOS') mv_invisiveis
from produtos_estoque;
-- medido 08/08/2026: 6739 / 1203 / 979 / 347 / 879 / 428 (48,7%)
drop table if exists _emb_watch;
create table _emb_watch as select id_unico, md5(embedding::text) h, estoque, now() t0
from produtos_estoque where embedding is not null and estoque > 0;
```

**P4.3 — CONTROLE POSITIVO, pré-fix** `[AJUSTADO — obrigatório]`. Escolher `id_unico` X de loja ativa (244033/220540/94134), guardar o md5 de todas as linhas vendáveis daquela loja, e forçar portador + vítimas (as duas perturbações se auto-curam no ciclo seguinte, porque o sync reescreve nome e estoque a partir do ERP):
```sql
update produtos_estoque set nome = nome || $$ ZZTESTE$$ where id_unico = $$<X>$$;      -- portador (rename -> re-embed)
update produtos_estoque set estoque = estoque + 1 where id_loja = $$<loja>$$ and estoque > 0;  -- vitimas em toda pagina
```
Esperar 1 ciclo. **ESPERADO PRÉ-FIX: os nulos daquela loja SOBEM.** Sem isso não existe baseline e o P4.5 passa mesmo com fix incompleto.

**P4.4 — FIX (a): lotes homogêneos + `.order`.** `sync_erp.py:365-369`:
```python
                if batch_upsert:
                    t0 = time.perf_counter()
                    # O postgrest-py monta ?columns=<UNIAO das chaves do lote>. Num lote heterogeneo o
                    # registro que NAO traz 'embedding' e gravado com NULL nessa coluna (o default da
                    # coluna e NULL, logo Prefer: missing=default tambem nao salva — ja testado).
                    # Foi assim que 1.203 vetores foram apagados. Agrupar por conjunto de chaves resolve
                    # a CLASSE do bug. NUNCA volte a fazer um upsert unico com shapes diferentes.
                    grupos = defaultdict(list)
                    for _reg in batch_upsert: grupos[frozenset(_reg.keys())].append(_reg)
                    for _lote in grupos.values():
                        supabase_client.table("produtos_estoque").upsert(_lote).execute()
                    t_upsert_total += time.perf_counter() - t0
                    total_upserts += len(batch_upsert)
```
`from collections import defaultdict` no topo. Os dois dict literais de registro (287-302, 307-322) têm chaves idênticas; a única condicional é `embedding` (335, 344) → hoje o agrupamento produz exatamente 2 lotes. O upsert de zeragem (389-391) tem shape próprio e já era homogêneo. **E** em `sync_erp.py:163`: `.order("id_unico")` antes do `.range(...)` com o comentário do por quê.

**P4.5 — Deploy isolado do commit 1 + prova.** Esperar ≥3 ciclos e rodar, junto com a **repetição** do P4.3:
```sql
select count(*) observadas,
       count(*) filter (where p.embedding is null) virou_null,
       count(*) filter (where p.embedding is not null and md5(p.embedding::text) <> w.h) vetor_mudou,
       count(*) filter (where p.estoque <> w.estoque) estoque_mudou
from _emb_watch w join produtos_estoque p using (id_unico);
```
**PASSA se `virou_null = 0` E `estoque_mudou > 0` E o log do mesmo ciclo mostrar `embeds_novos > 0`.** Deployar (a) junto com (b) destrói a atribuição causal: (b) preencheria nulos e mascararia um (a) incompleto.

**P4.6 — `embedding_text.py`** (novo, raiz): `MODELO_EMBEDDING`, `DIM_EMBEDDING = 768` e `texto_embedding_produto(nome) -> (nome or "").strip()`. Docstring registra: **apenas o nome, sem tamanho**, porque (i) o filtro de tamanho é SQL (`tamanho_tokens` + trigger), (ii) o vetor é por `id_produto` e linhas P/M/G compartilham o mesmo vetor — `"| Tamanho: GG"` gravava um vetor que **mente** sobre as outras (verificado: PIJAMA NADADOR com P, M e GG casando 1,0000 com o texto de GG), (iii) o lado de consulta (`bot.get_embedding`, retrieval_query) não tem sufixo de tamanho. E: **não** normalizar espaço interno (103 linhas com espaço duplo; colapsar invalidaria todo vetor já escrito). `.strip()` é no-op medido (0 linhas com espaço em borda).

**P4.7 — Sync usa o canônico.** `sync_erp.py:332` e `:341`: `vetor = get_embedding(texto_embedding_produto(base_nome))`. No-op comportamental hoje; o ganho é estrutural (um único lugar define a convenção).

**P4.8 — FIX (b): re-embed automático, cap por CHAMADAS.** Nova `carregar_ids_sem_embedding()` após sync_erp.py:188 — `select("id_unico").is_("embedding","null").order("id_unico").range(...)` paginado (**consulta separada de propósito**: trazer `embedding` em `carregar_estado_atual_do_banco` seriam ~65 MB de texto só para descobrir um booleano). Chamar após 210, logar `[EMB] N linhas sem vetor no inicio do ciclo`. Constante junto a 41: `REEMBED_MAX_POR_CICLO = int(os.getenv("SYNC_REEMBED_MAX","300"))`. Novo ramo **entre** 346 e 348 (depois dos ramos que já re-embedam, **antes** da comparação de estoque/preço e do `else: total_skipped += 1` que hoje deixa a linha nula parada para sempre):
```python
                        elif id_unico in ids_sem_embedding:
                            # RE-EMBED: linha existe, nome nao mudou, vetor NULL. Duas origens:
                            # (1) lote heterogeneo antigo apagou; (2) get_embedding() falhou no insert
                            # e ninguem nunca voltou. Sem este ramo a linha fica invisivel para a RPC
                            # PARA SEMPRE (a deteccao abaixo so olha estoque/preco/grupo).
                            _txt = texto_embedding_produto(base_nome)
                            # O cap protege contra rajada de CHAMADAS, nao contra linhas: 979 linhas nulas
                            # com estoque = 293 textos distintos; o embedding_cache (linha 44) atende as
                            # outras 686 de graca. Contar linhas fazia a cura levar 7 ciclos em vez de 1.
                            if _txt in embedding_cache or total_reembed_chamadas < REEMBED_MAX_POR_CICLO:
                                if _txt not in embedding_cache: total_reembed_chamadas += 1
                                t0 = time.perf_counter(); vetor = get_embedding(_txt)
                                t_embed_total += time.perf_counter() - t0
                                if vetor: reg["embedding"] = vetor; total_reembeds += 1
                            batch_upsert.append(reg)
```

**P4.9 — GUARD de zeragem, com alarme real.** `paginas_incompletas = False` antes de 219; `= True` antes dos `break` de 240-242 e 373-375. Linha 385: `if ids_para_zerar and not paginas_incompletas:` … `elif ids_para_zerar:` → `log.error("[GUARD] N ids seriam zerados mas alguma pagina do ERP falhou — zeragem ABORTADA")`, `ciclo_ok = False` **e** persistir o estado degradado onde o processo web enxergue (`bot_settings`), porque com o cron dedicado as globais morrem `[AJUSTADO]`. Linha 397: `LAST_RUN_OK = ciclo_ok`. **E em `bot.py:2067-2093`, acrescentar `sync_erp.LAST_RUN_OK is False` (ou o campo persistido) como segunda condição de alarme** — sem isso o guard nasce mudo. Trade-off aceito: um ciclo com página falhando deixa estoque otimista por 10 min; o dano oposto (zerar centenas de linhas boas, em silêncio, hoje) é muito maior.

**P4.10 — Contadores no heartbeat.** `sync_erp.py:49-51` + `global` da 192 + 398: `LAST_RUN_EMB_NULOS`, `LAST_RUN_REEMBEDS`, e `LAST_RUN_INFO` ganhando `emb_nulos_inicio:` e `reembeds:`; incluir também no `log.info` final (399-404).

**P4.11 — Deploy (b)+(c)+guard e cura automática.** A cada ciclo, 3x, a query de contagem do P4.2. **Esperado: `nulos_com_estoque` de 979 → ~0 e `mv_invisiveis` de 428 → ~0 em 1-2 ciclos** `[AJUSTADO — era "~3 ciclos"]`. Isto **não** viola a restrição "backfill só depois de padronizar o texto": preenche com `texto_embedding_produto`, que **é** a convenção canônica. Depois: `drop table _emb_watch;`

**P4.12 — `tests/test_sync_upsert_homogeneo.py`** (sem DDL, `sys.path` na raiz, tabela criada uma vez pela management API): Teste 1 **reproduz** o apagamento com lote heterogêneo (se **não** reproduzir, a versão instalada mudou e o pin está errado); Teste 2 preserva os dois vetores com o agrupamento por `frozenset(keys)`.

**P4.13 — `regenerar_embeddings.py`.** (1) import do módulo canônico; (2) linha 52: `.select("id_produto, nome").order("id_produto")` — remover `tamanho`/`preco` é **estrutural**, sem a coluna ninguém reintroduz o tamanho; (3) linhas 81-88: `texto = texto_embedding_produto(produto.get('nome'))` + `skip` se vazio, com o comentário do por quê (0,876 de cosseno contra o canônico, num ranking cujo top-12 cabe em 0,018); (4) `request_options={"timeout": 30}` (hoje é o único caminho sem timeout); (5) cache por texto; (6) `--skip=N` para retomada (determinística graças ao `.order`); (7) **dedupe e escrita por `(id_produto, nome)`** `[AJUSTADO]` — `.eq("id_produto", ...).eq("nome", ...)`; (8) comentário-aviso de **restrição dura** mantendo `update` com dict único (nunca upsert em lote). **Nunca** rodar o script antes deste passo: sem (3), `--only-missing` sobrescreveria 1.927 linhas com vetor BOM.

**P4.14 — Backup da coluna (único rollback do re-embed).** `create table embedding_backup_20260808 as select id_unico, embedding from produtos_estoque where embedding is not null;` (~5.536 linhas, ~17 MB).

**P4.15 — Re-embed CANÔNICO TOTAL.** `python regenerar_embeddings.py` (**sem** `--only-missing`), retomada com `--skip=<último i>`. **Por que total:** a base está com convenção **mista** e `--only-missing` por definição não toca linha não-nula. Amostra de 12: 7 canônicos (1,0000) e 5 contaminados (0,9296/0,9259/0,9182/0,8848/0,8571) — ~40% da base, e os contaminados são o **core** de lingerie. Detectar qual está errado custa a mesma chamada que corrigir. ~1.576 chamadas × 1,3s + UPDATEs + pausas ≈ **40-50 min**. Conferir `git log -1 --stat regenerar_embeddings.py` antes de rodar (sem o P4.13 mergeado, este passo **contamina** a base inteira). **Bot desligado → sem janela a negociar.**

**P4.16 — `REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx; ANALYZE produtos_estoque;`** `[AJUSTADO]`. Medir `pg_relation_size` antes e depois.

**P4.17 — Provas.** (A) contagem final: `nulos_com_estoque = 0`, `mv_invisiveis = 0`. (B) **spot-check** de `'baby doll de renda'` (RETRIEVAL_QUERY, 768) na MATRIZ: baseline 1º `10BR BABY DOOL DE RENDA` 0,7781 … 12º 0,7605 (spread 0,0176); pós-fix o bestseller continua no top-3 **com similaridade maior** (hoje o vetor dele está a 0,8757 do canônico; 3 das 12 linhas são NULL) e nenhuma categoria estranha entra no top-12. (C) **recall** `[AJUSTADO]`: rodar o mesmo top-12 na forma aproximada (`order by embedding <=> v`, elegível ao HNSW) e na forma exata (`order by 1-(embedding <=> v) desc`) e exigir **conjuntos idênticos** — divergência aponta índice degradado (rodar P4.16), não regressão do texto. (D) convenção unificada: 10 id_produto sorteados, embedar `nome` puro e exigir min/max de similaridade ≥ 0,9999 (hoje ~40% devolve 0,857-0,930). (E) `python tests/eval/run_eval.py` 18/18 — **obrigatório** aqui, é a mudança com maior chance de mexer em cenário de curadoria.

**P4.18 — `DDL` migration 0012 `embeddings_health()`** (renumerada):
```sql
create or replace function public.embeddings_health()
returns table (linhas_total bigint, nulos_total bigint, nulos_com_estoque bigint,
               produtos_com_nulo bigint, nulos_mais_vendidos bigint)
language sql stable as $fn$
  select count(*), count(*) filter (where embedding is null),
         count(*) filter (where embedding is null and estoque > 0),
         count(distinct id_produto) filter (where embedding is null),
         count(*) filter (where embedding is null and estoque > 0
                          and upper(coalesce(nome_grupo,'')) = 'PRODUTOS MAIS VENDIDOS')
  from public.produtos_estoque;
$fn$;
grant execute on function public.embeddings_health() to anon, authenticated, service_role;
NOTIFY pgrst, 'reload schema';
```
Grant a `anon` é **obrigatório** (chave do bot é anon). RPC e não coluna gerada: coluna obrigaria rewrite da tabela e depende de qual processo roda o sync.

**P4.19 — `/health` + watchdog** (**depois** do C1.4 da F1). `EMB_NULL_ALERT_MAX = int(os.getenv("EMB_NULL_ALERT_MAX","20"))`, `_embeddings_health(ttl_seg=600)` com cache (o `/health` é sonda de liveness do Railway — não pode virar 1 query por probe) e try/except devolvendo o último valor; no body: `"embeddings"`, `"sync_emb_nulos_inicio"`, `"sync_reembeds"`. **DECISÃO EXPLÍCITA: não mexer em `healthy`/status HTTP** — problema de qualidade de dado não pode causar restart de container; o canal é o alarme. Novo job `_verificar_embeddings_e_alertar` espelhando `_verificar_sync_e_alertar` e reusando `_disparar_alerta`, `interval` 60 min, reenvio a cada 12h, mensagem **comercial** ("o cliente não encontra esses produtos"). Limiar 20 = folga para falha transitória (regime normal após P4.11 é 0).

**P4.20 — Testar o alarme uma vez** (`EMB_NULL_ALERT_MAX=-1` temporário, confirmar recebimento, devolver a 20 **no mesmo turno de trabalho**) e limpar: `drop table if exists _emb_watch;`, `drop table if exists _test_emb_upsert;`. Manter `embedding_backup_20260808` ~2 semanas.

### Commits F4
1. `fix(sync): lotes homogeneos no upsert + .order na leitura de estado — para de apagar embedding` (+ teste de biblioteca + pin)
2. `refactor(embed): convencao canonica em modulo unico + hardening do regenerar (dedupe por id_produto+nome, .order, timeout, cache, --skip)`
3. `feat(sync): re-embed automatico com cap por chamadas + guard de zeragem que alerta + contadores no heartbeat`
4. `feat(health): metrica de embeddings nulos + alarme` (migration 0012) — **após C1.4**

### Fora de escopo declarado (com a razão que fecha a questão)
**"Segundo mecanismo" não existe** — refutado com dado. A quebra da curva em 20+ (34,7% vs 60,8%) é artefato de medição: o envenenamento é por **lote**, e o lote é uma página do ERP por loja, logo todas as variações de um produto numa loja são apagadas juntas. Contando por cluster `(id_produto, id_loja)` e excluindo a loja morta 112102 a curva volta a ser monotônica (15,1% / 19,7% / 42,4% / 52,0% / 70,5% / 67,2%, os dois últimos empatados no ruído). Duas provas independentes do mecanismo único: (i) a loja 112102 tem 1.318 linhas e **zero** nulos porque não recebe produto novo desde 03/2026 — sem produto novo não há portador; (ii) nas 3 lojas ativas a página 1 (produtos mais novos) fica em 0,8-0,9% de nulos enquanto as páginas 2+ vão de 12% a 41% — **o produto novo é o portador, não a vítima**. Há uma terceira *origem* (não mecanismo): `get_embedding` devolvendo None no insert — fechada pelo P4.8.

---

## 6. FRENTE 5 — RANKING COMERCIAL `DDL` + `CÓDIGO`  — **VERSÃO CORRIGIDA (revisor reprovou)**

### Correções aplicadas
- `[AJUSTADO — AJUSTE PRINCIPAL]` **A janela absoluta de cosseno NÃO escopa categoria.** Reproduzido na base real: com `filtro_tamanho` preenchido o `sim_max` desaba e a janela de 0,05 engole o pool inteiro — `fantasia`+`G`/MATRIZ devolvia **6 de 8 produtos em tier 1**, todos CALCINHA/PIJAMA, promovidos por estoque **acima** da categoria pedida e **com `destaque=true`**. Era a restrição dura violada com selo de bestseller. O escopo passa a ser **lexical** (token do termo no `nome`, ou head-noun igual ao do produto de `rn_sim=1`) **além** da janela. Baixar a janela para 0,04 **não resolve** — o corte relativo fica abaixo do pior item do pool para qualquer valor plausível.
- `[AJUSTADO]` `destaque := (tier = 1)`, não `nome_grupo = 'PRODUTOS MAIS VENDIDOS'`. Medido: `CONJUNTO MOANA` saía com `destaque=true` em tier 2 numa busca de fantasia — e a linha 51 do prompt **proíbe** chamar CONJUNTO de fantasia.
- `[AJUSTADO]` `estoque_grade` dos bestsellers passa a ser a **grade verdadeira** por subquery escalar (poucas linhas, usa o índice novo), não a soma sobre o pool. Resolve os dois defeitos de uma vez: o valor subestimado (9 no pool vs 15 na tabela) e o colapso para 1 tamanho quando há `filtro_tamanho` (que fazia `minimo_grade=3` derrubar 15 dos 74 pares bestseller/G na MATRIZ).
- `[AJUSTADO]` `_ids_ja_mostrados()` é **definida no mesmo commit** e com guard defensivo. O plano original inseria a chamada dentro do `try` de bot.py:351-372, cujo `except Exception` devolve *"Erro ao consultar banco de dados"* → `NameError` viraria apagão silencioso de **100% das buscas**, com diagnóstico enganoso.
- `[AJUSTADO + DECISÃO]` **Escopo do "já mostrado" = CONVERSA** (não turno), com TTL de 30 min e teto de 15 ids, **snapshot congelado antes do laço de retry** (bot.py:1401-1412) para não quebrar idempotência. O plano deixava o escopo indefinido; escopo de turno não serve para nada, porque *"tem mais?"* é sempre turno novo. **Fallback obrigatório:** se a busca com exclusões devolver <3 produtos, repetir **sem** exclusões e logar — senão trocamos repetição por *"não achei nada"*.
- `[AJUSTADO]` A5/A6 mediam a **fronteira da tool**, não o card que chega no WhatsApp: com a suíte verde o defeito do dono da loja continuaria. Agora há assert e2e em `tests/eval` e A5 conta bestseller **da categoria pedida** (possível porque `destaque` = tier 1). A4 passa a exigir que o #1 de similaridade pura esteja entre os `ancora_semantica` primeiros (o boost de palavra-chave reordena dentro do tier 0 — A4 original falharia sem nada errado).
- `[AJUSTADO]` Números esperados da verificação corrigidos: `camisola`/MATRIZ dá `10C CAMISOLA DE URDA` com `estoque_grade` **30** e `10CR ... RENDA` **24** (47/32 eram totais da tabela, não do pool); com tamanho `M` caem para 15.
- `[AJUSTADO]` Migration renumerada para **0013**; `migrations/README.md:20` (smoke test com a assinatura de 3 args, dropada na F2) atualizado; regra anti-overload escrita no README.
- `[DECISÃO]` **A F5 também popula `excluir_ids`** (o plano da F5 empurrava para a F2, e a F2 declarava fora de escopo — ambiguidade resolvida aqui): é na F5 que o dedupe + pool maior tornam a variedade possível.
- `[BLOQUEADO]` Agregação de atacado e `minimo_grade` — default implementado (campeões primeiro; profundidade só desempata; mínimo 3), parametrizável, mas o valor é decisão comercial.

### Passos
**P5.1 — Gate de pré-condição (F4 concluída).** `select count(*) linhas, count(*) filter (where embedding is null) sem_vetor from produtos_estoque where nome_grupo='PRODUTOS MAIS VENDIDOS' and estoque>0;` **CRITÉRIO: `sem_vetor = 0`** e, por loja, zero bestsellers 100% invisíveis (medido hoje: MATRIZ 36/92 = 39%, FILIAL01 33/96, FILIAL02 22/111). A F5 pode subir com base furada sem quebrar nada, mas **não se pode declarar a P1 resolvida** — registrar o número no commit.

**P5.2 — Backup das definições.** `migrations/backup/backup_rpc_2026-08-08.sql` com a saída de `pg_get_functiondef` das sobrecargas existentes. Não sobrescrever `backup_rpc_current.sql` (defasado).

**P5.3 — `DDL` migration 0013.**
```sql
-- Migration 0013: ranking comercial dentro da selecao + dedupe por id_produto + excluir_ids
-- Pre-requisitos: 0001, 0002, 0009, 0011 (a sobrecarga de 3 args JA foi dropada pela 0011).
-- REGRA: nenhuma migration posterior pode recriar a assinatura de 5 args (duas candidatas
-- fazem o PostgREST responder 'Could not choose the best candidate function').

CREATE INDEX IF NOT EXISTS idx_produtos_estoque_id_produto ON public.produtos_estoque (id_produto);

-- normalizador para comparacao lexical insensivel a acento (mesmo translate de normalize_tamanho_tokens)
CREATE OR REPLACE FUNCTION public.norm_busca(txt text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
  SELECT upper(translate(coalesce(txt,''),
    'ÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑáàâãäåéèêëíìîïóòôõöúùûüçñ',
    'AAAAAAEEEEIIIIOOOOOUUUUCNAAAAAAEEEEIIIIOOOOOUUUUCN'));
$f$;

DROP FUNCTION IF EXISTS public.buscar_produtos_semantico(vector, double precision, integer, text, text);

CREATE FUNCTION public.buscar_produtos_semantico(
    query_embedding vector, match_threshold double precision, match_count integer,
    filtro_tamanho text DEFAULT NULL, filtro_id_loja text DEFAULT NULL,
    excluir_ids text[] DEFAULT NULL, termo_tokens text[] DEFAULT NULL,
    limite_produtos integer DEFAULT 8, ancora_semantica integer DEFAULT 2,
    janela_similaridade double precision DEFAULT 0.05, minimo_grade numeric DEFAULT 3)
RETURNS TABLE(id_unico text, id_produto text, id_loja text, loja text, nome text, tamanho text,
    preco numeric, preco_varejo numeric, preco_atacado numeric, estoque numeric,
    grupo_id text, nome_grupo text, similarity double precision,
    estoque_grade numeric, n_tamanhos integer, destaque boolean, tier smallint)
LANGUAGE sql STABLE
-- KNN EXATO de proposito: com filtro (loja/tamanho/estoque) o HNSW devolve MENOS linhas que o pool
-- pedido (medido: 13-48 de 60). Exato devolve 60 sempre, em 10-16 ms (tabela tem 6.739 linhas).
-- Trocar por SET hnsw.iterative_scan='relaxed_order' se a tabela passar de ~50k linhas.
SET enable_indexscan = off
SET enable_bitmapscan = off
AS $function$
WITH tokens_alvo AS (SELECT public.normalize_tamanho_tokens(filtro_tamanho) AS toks),
cand AS (
  SELECT p.id_unico, p.id_produto, p.id_loja, p.loja, p.nome, p.tamanho, p.preco,
         p.preco_varejo, p.preco_atacado, p.estoque, p.grupo_id, p.nome_grupo,
         1 - (p.embedding <=> query_embedding) AS sim
  FROM produtos_estoque p, tokens_alvo
  WHERE p.estoque > 0 AND p.embedding IS NOT NULL
    AND (filtro_tamanho IS NULL OR cardinality(tokens_alvo.toks) = 0
         OR p.tamanho_tokens && tokens_alvo.toks)
    AND (filtro_id_loja IS NULL OR p.id_loja::text = filtro_id_loja)
    -- ATENCAO: 'x <> ALL(NULL::text[])' devolve NULL e DESCARTA a linha (verificado).
    -- Por isso o guard de NULL/vazio + NOT (= ANY), nunca <> ALL.
    AND (excluir_ids IS NULL OR cardinality(excluir_ids) = 0
         OR NOT (p.id_produto = ANY (excluir_ids)))
    AND 1 - (p.embedding <=> query_embedding) > match_threshold
  ORDER BY p.embedding <=> query_embedding
  LIMIT GREATEST(match_count, limite_produtos)),
cg AS (SELECT c.*, SUM(c.estoque) OVER (PARTITION BY c.id_produto) AS grade_pool,
              COUNT(*) OVER (PARTITION BY c.id_produto) AS n_tam FROM cand c),
rep AS (  -- 1 linha por id_produto. preco e UNICO por (id_produto,id_loja): 2334 pares verificados.
  SELECT DISTINCT ON (id_produto) * FROM cg ORDER BY id_produto, estoque DESC, sim DESC, id_unico),
rk AS (SELECT r.*, MAX(r.sim) OVER () AS sim_max,
              ROW_NUMBER() OVER (ORDER BY r.sim DESC, r.id_produto) AS rn_sim,
              -- grade VERDADEIRA, calculada so para bestseller (poucas linhas, usa o indice novo)
              CASE WHEN coalesce(r.nome_grupo,'') = 'PRODUTOS MAIS VENDIDOS'
                   THEN (SELECT sum(pe.estoque) FROM produtos_estoque pe
                         WHERE pe.id_produto = r.id_produto AND pe.estoque > 0
                           AND (filtro_id_loja IS NULL OR pe.id_loja::text = filtro_id_loja))
                   ELSE r.grade_pool END AS grade_real
       FROM rep r),
top1 AS (SELECT split_part(public.norm_busca(nome), ' ', 1) AS head FROM rk WHERE rn_sim = 1),
cl AS (
  SELECT rk.*,
    CASE
      -- tier 0: ancora semantica. A categoria pedida nunca perde o topo.
      WHEN rn_sim <= ancora_semantica THEN 0::smallint
      -- tier 1: bestseller do ERP, DENTRO da janela, com grade minima E DENTRO DA CATEGORIA.
      -- O escopo de categoria e LEXICAL: a janela de cosseno NAO escopa (medido: com
      -- filtro_tamanho o sim_max desaba e 0,05 engole o pool inteiro).
      WHEN coalesce(rk.nome_grupo,'') = 'PRODUTOS MAIS VENDIDOS'
           AND rk.sim >= rk.sim_max - janela_similaridade
           AND rk.grade_real >= minimo_grade
           AND ( (termo_tokens IS NOT NULL AND cardinality(termo_tokens) > 0
                  AND EXISTS (SELECT 1 FROM unnest(termo_tokens) t
                              WHERE public.norm_busca(rk.nome) LIKE '%' || public.norm_busca(t) || '%'))
              OR split_part(public.norm_busca(rk.nome), ' ', 1) = (SELECT head FROM top1) )
        THEN 1::smallint
      ELSE 2::smallint END AS tier
  FROM rk)
SELECT id_unico, id_produto, id_loja, loja, nome, tamanho, preco, preco_varejo, preco_atacado,
       estoque, grupo_id, nome_grupo, sim AS similarity, grade_real AS estoque_grade,
       n_tam::integer AS n_tamanhos,
       (tier = 1) AS destaque,          -- selo = PROMOVIDO NA CATEGORIA, nao pertencer ao grupo
       tier
FROM cl
ORDER BY tier ASC, (CASE WHEN tier = 1 THEN grade_real ELSE 0 END) DESC, sim DESC, id_produto
LIMIT limite_produtos;
$function$;

NOTIFY pgrst, 'reload schema';
```
**Defaults medidos:** pool 60 (18-36 produtos distintos, contra 4-9 com 10) · `limite_produtos` 8 (1598 tokens vs 2061 das 10 linhas de hoje) · `ancora_semantica` 2 (em `cueca boxer` a janela não pega nada — top 0,846, resto ≤0,735 — e sem âncora o resultado degradaria) · `janela_similaridade` 0,05 (distância do melhor bestseller ao topo: 0,016-0,027 em baby doll/camisola/pijama/sutiã, 0,05-0,07 em fantasia) · `minimo_grade` 3 `[BLOQUEADO — número escolhido, não medido]`.

**P5.4 — Rollback.** `migrations/rollback/rollback_0013_...sql`: `DROP FUNCTION` da de 11 args + `CREATE OR REPLACE` da de 5 args copiado **literalmente** de `0009` linhas 8-64 + `NOTIFY pgrst, 'reload schema';`. **Não** recriar a sobrecarga de 3 args. `idx_produtos_estoque_id_produto` **fica** (aditivo e útil: hoje a query de `consultar_produto_por_id` é Seq Scan descartando 6.730 linhas).

**P5.5 — Smoke SQL antes de tocar em bot.py.** (a) `select count(*) from pg_proc where proname='buscar_produtos_semantico'` = **1**. (b) forma/dedupe: ≤8 linhas, `count(distinct id_produto) = count(*)`, 1ª linha = semente com tier 0, tiers não-decrescentes. (c) `excluir_ids` remove o id e o pool se recompõe até 8. (d) **REGRESSÃO CRÍTICA DO NULL**: `count(*)` com 5 args == `count(*)` com `excluir_ids => null`; se a segunda vier 0, toda busca sem exclusões volta vazia **em silêncio**. (e) `filtro_tamanho => 'G'` traz linhas `'G/GG'`. (f) `[AJUSTADO]` **anti-vazamento de categoria**: `fantasia` + `filtro_tamanho => 'G'` + `termo_tokens => array['fantasia']` na MATRIZ **não** pode trazer nenhuma CALCINHA/PIJAMA em tier 1 (antes da correção: 6 de 8).

**P5.6 — bot.py: parâmetros da RPC + `excluir_ids`.** Constantes junto a bot.py:70-72: `POOL_CANDIDATOS = 60`, `LIMITE_PRODUTOS = 8`, `EXCLUIR_TTL_SEG = int(os.getenv("EXCLUIR_TTL_SEG","1800"))`, `EXCLUIR_MAX_IDS = int(os.getenv("EXCLUIR_MAX_IDS","15"))`, `_mostrados = {}`, `_mostrados_lock = threading.Lock()`. Funções **neste commit** `[AJUSTADO]`:
```python
def _registrar_mostrados(user_id, ids):      # chamado no render, junto de cada card
def _ids_ja_mostrados(user_id):
    """list[str] de id_produto mostrados a ESTE cliente na conversa recente (TTL EXCLUIR_TTL_SEG,
    teto EXCLUIR_MAX_IDS, mais recentes primeiro). [] com guard defensivo: NUNCA pode levantar."""
```
Em `_executar_turno`, **antes** do laço de retry (bot.py:1401): `_turn_ctx.excluir_ids = _ids_ja_mostrados(user_id)` — snapshot congelado, imune ao retry de 3 tentativas. Em bot.py:353-360:
```python
        rpc_params = {'query_embedding': vetor_busca,
            'match_threshold': 0.5,           # inerte de proposito: em 'fantasia' o melhor match e 0,647
            'match_count': POOL_CANDIDATOS, 'filtro_tamanho': tamanho_alvo,
            'filtro_id_loja': id_loja_alvo, 'limite_produtos': LIMITE_PRODUTOS,
            'termo_tokens': palavras_chave or None,                       # escopo lexical da tier 1
            'excluir_ids': (getattr(_turn_ctx, "excluir_ids", None) or None)}
```
**Fallback obrigatório** `[AJUSTADO]`: se após o re-filtro `len(selecao) < 3` **e** havia exclusões, repetir a RPC sem `excluir_ids`, logar `[EXCLUIR] fallback sem exclusoes` e refletir em `filtro_aplicado["excluir_ids_aplicados"] = 0`. **Não** expor `excluir_ids` como parâmetro da tool: as tools são registradas passando os callables com automatic function calling, o SDK deriva o schema da **assinatura**, e o modelo passaria a inventar exclusões. **Nunca** aplicar exclusões em `consultar_produto_por_id` (restrição dura: quebraria "manda de novo aquele").

**P5.7 — Remover o boost de bestseller e ordenar pelo tier.** `bot.py:378-392`: apagar `GRUPO_PRIORITARIO` (378) e o `score_boost += 10` (386-387); **manter** o boost de palavra-chave (`+= 1`); trocar a ordenação por `produtos_candidatos.sort(key=lambda x: (x.get('tier', 0), -x.get('_score_boost', 0)))`. Sort estável: o tier do SQL manda, a palavra-chave desempata **dentro** do tier, a ordem do SQL (grade/similaridade) sobrevive entre iguais. Se a RPC antiga estiver no ar, `tier` vem ausente → 0 → comportamento idêntico ao de hoje (degradação graciosa). Ordenar por `-boost` antes do tier faria uma palavra-chave promover item tier 2 acima da âncora e o ranking comercial viraria decorativo.

**P5.8 — Dedupe final por `id_produto`.** `bot.py:442-449`: iterar `validados` inteiro, chave `str(p.get('id_produto'))`, `break` em `len(selecao) >= LIMITE_PRODUTOS`. O dedupe por `id_unico` é inerte (é a PK). Efeito colateral bom: `extrair_produtos_de_tool_results` (bot.py:1080) indexa por `id_produto` com last-write-wins — com 1 linha por produto o cache do card passa a ser determinístico. `renderizar_mensagem_estruturada` tem cap de 5 cards e o prompt limita a 3 recomendações → 8 candidatos **não** alongam o turno (restrição dura da F1 preservada).

**P5.9 — Enxugar o payload.** bot.py:451-457, após o loop de `tem_foto`: **logar** uma linha por produto com `tier/destaque/estoque_grade/similarity` e então remover de cada item `('id_loja','loja','grupo_id','nome_grupo','similarity','tier','_score_boost','estoque_grade','n_tamanhos')`. **MANTER** `id_produto, id_unico, nome, tamanho, preco, preco_varejo, preco_atacado, estoque, destaque, imagem, tem_foto, n_fotos` **e as chaves de topo `filtro_aplicado`/`aviso` da F2** `[AJUSTADO — integração entre frentes]`. Não remover `preco` (`_legenda_card` o usa) nem `imagem`/`n_fotos` (F3). `destaque` (booleano) substitui `nome_grupo` — mesma informação sem vazar rótulo interno, e agora significa "promovido **na categoria**".

**P5.10 — `tests/test_ranking_comercial.py`.** 7 termos × 2 lojas, vetor cacheado em arquivo local. **A1** status sucesso. **A2** ≤8 produtos, todos `id_produto` distintos (hoje `baby doll`/244033 devolve 10 linhas / 5 produtos). **A3** nenhum campo interno no payload. **A4 `[AJUSTADO]`** o #1 de similaridade pura está entre os `ancora_semantica` primeiros. **A5 `[AJUSTADO]`** ≥12 dos 14 pares com ao menos 1 produto `destaque=True` (medido 13/14 contra 6/14 hoje). **A6** mediana de produtos distintos = 8 (hoje 5). **A7** com exclusões, a 2ª chamada não repete nenhum `id_produto` e ainda devolve ≥5. **A8 `[NOVO]`** nenhum produto tier 1 cujo head-noun difira do head-noun do `rn_sim=1` — é o assert que impede a regressão do `fantasia`+G.

**P5.11 — Gate e2e na eval** `[AJUSTADO]`. Cenários `fantasia`+G e `baby doll`+G verificando (i) `recommended_only_in_category` (run_eval.py:306) — nenhum id de outra categoria em `produtos_recomendados`; (ii) quando a lista da tool tem item `destaque=true` **da categoria pedida**, esse id aparece em `produtos_recomendados`. Suíte completa depois dos P5.6-P5.9; atenção a `curadoria` e `happy-path` (a lista agora tem 8 opções distintas, o modelo tem mais chance de escolher errado). Se cair, **o knob é o escopo lexical / a janela, não o assert e nunca o prompt**.

**P5.12 — Custo em produção (pós-religar).** Base medida hoje (14 turnos com busca): `tokens_in` médio 13.663, `tokens_out` 100, p50 10.091 ms, p95 80.894 ms. Esperado: `tokens_in` ~450 **menor** por chamada; p50 no máximo ~15 ms acima (custo Postgres do ranking: 13-21 ms, contra 67-100 ms da variante com lateral join, descartada). `tokens_in` subindo = algum campo interno voltou ao payload (rever P5.9).

**P5.13 — Texto para a F6** (não deployar aqui). Ver §8.

### Commits F5 (1 e 2 na MESMA janela de deploy)
1. `feat(busca): RPC de ranking comercial — dedupe, tier escopado por categoria, excluir_ids, KNN exato, indice em id_produto` (migration 0013 + rollback + backup)
2. `feat(busca): bot.py consome o ranking — pool 60, corte em 8, excluir_ids por conversa, payload enxuto`
3. `test(busca): invariantes A1-A8 + cenarios e2e de categoria`

O commit 1 isolado não quebra (a RPC nova atende a chamada de 5 parâmetros; defaults preenchem o resto) mas já liga o dedupe com pool 10 — melhoria pequena, não o ganho da frente.

### Como PROVAR
- Antes/depois no mesmo termo, em SQL, sem passar pelo bot: `baby doll`/MATRIZ hoje 10 linhas / 5 produtos / 0 bestseller → esperado 8 linhas / 8 produtos / 1 bestseller (`BABY DOLL AYLA 895`, tier 1, grade 29). `camisola`/MATRIZ hoje 4 produtos / 0 bestseller → esperado 8 produtos com `10C CAMISOLA DE URDA` (grade **30**) e `10CR CAMISOLA DE URDA RENDA` (grade **24**) em tier 1 `[AJUSTADO]`.
- **Degradação graciosa**: `cueca boxer`/MATRIZ não tem bestseller na janela (o melhor está a 0,15) → `CUECA BOXER TRIFIL` em 1º (tier 0) e o resto por similaridade pura (tier 2). Se vier 1-2 produtos, a âncora foi perdida.
- `EXPLAIN (ANALYZE)`: a linha do plano é **Seq Scan**, não `Index Scan using produtos_estoque_embedding_idx`, e o pool devolve o número pedido.
- A8 + o cenário e2e de `fantasia`+G: prova o ajuste principal.

---

## 7. GATES DE VERIFICAÇÃO — QUANDO RODAR O QUÊ

### 7.1 `tests/eval/run_eval.py`
| Momento | Comando | Critério |
|---|---|---|
| **Antes de qualquer commit** | `run_eval.py` → `tests/eval/baseline_frente1_antes.csv` | Registrar pass/fail por cenário. Falha existente = pré-existente, não regressão |
| Iteração barata | `run_eval.py --no-judge` / `--dimension X` / `--only <id>` | sem custo de juiz |
| Antes de **cada** commit de `bot.py` (F1 C1.1/C1.2/C1.3, F2, F3, F5) | `run_eval.py` completo | zero FALHA GRAVE nova vs baseline |
| F1 C1.2 | `--dimension tamanho` primeiro | 2 cenários do guard |
| F1 C1.3 | suíte inteira | é o commit que mexe no contexto multi-turno |
| F4, **obrigatório** após o re-embed total | `run_eval.py` | 18/18; comparar com `eval_results_20260714_203525.csv` |
| F5 após P5.6-P5.9 | `run_eval.py` | `recommended_only_in_category` e `stays_on_requested_category` |
| **F6, após o deploy do prompt** | `run_eval.py` + **novo baseline** | o prompt vive no banco → todo baseline anterior vira histórico |
| Fail isolado de juiz | reexecutar só o cenário com `--only` | variância de juiz antes de concluir regressão |

**Sempre junto:** `python tests/test_scenarios.py` e `python tests/test_cloud_migration.py` (dirigem `message_buffers` + `process_and_respond`).

### 7.2 Testes de unidade por frente
`tests/test_turn_hardening.py` (T0-T4) · `tests/test_tamanho_tokens.py` · `t_fotos.py` (A-G) · `tests/test_sync_upsert_homogeneo.py` · `tests/test_ranking_comercial.py` (A1-A8) · `tests/stress_test.py` (uma vez, F1).

### 7.3 SQL que prova cada frente
| Frente | Consulta | Antes (medido) | Depois (critério) |
|---|---|---|---|
| F1 | pares de concatenação com `gap <= 120` (query do §2) | 133 pares / parte dos 72 usuários | **0** |
| F1 | `/health` → `buffers_pendentes` em repouso | (novo) | **0** |
| F2 | `n_dropados_igualdade_legado > 0` em `tool_filtro_eventos`, `user_id not like 'eval_%'` | UNICO 623/674 derrubados | linhas salvas > 0 e nenhum vazio falso |
| F2 | `count(*) from pg_proc where proname='buscar_produtos_semantico'` | 2 | **1** |
| F3 | `card_envios` pós-smoke: 1 row por card + 1 por extra | 27 rows totais | ids corretos, URLs distintas |
| F4 | `nulos_com_estoque` / `mv_invisiveis` | **979 / 428** | **0 / 0** |
| F4 | `_emb_watch` join: `virou_null` (com `estoque_mudou>0` **e** `embeds_novos>0`) | controle positivo apaga | **`virou_null = 0`** |
| F4 | similaridade de `nome` puro vs vetor gravado, 10 produtos | ~40% em 0,857-0,930 | **todos ≥ 0,9999** |
| F5 | produtos distintos por busca (mediana, 14 pares) | **5** | **8** |
| F5 | pares com ≥1 bestseller **da categoria** | **6/14** | **≥12/14** |
| F5 | tier 1 com head-noun ≠ do top-1 (`fantasia`+G) | 6 de 8 | **0** |
| F5 | `avg(tokens_in)` em `bot_turns` com busca | 13.663 | ~450 menor; p50 ≤ +15 ms |

### 7.4 Verificações que ficam PENDENTES de religar o bot (Gate 0.5)
Smoke de fotos (P3.13) · `[TURNO] espera_lock=` no log · SQL de fechamento da F1 (24-48h de tráfego real) · volume de `tool_filtro_eventos` de produção · custo real da F5 · teste do alarme da F4 (esse pode ser feito com o bot ligado só no processo web, sem número ativo).

---

## 8. FRENTE 6 — DEPLOY ÚNICO DE PROMPT `PROMPT`

Um único `inserir_prompt.py`, **depois** de F1-F5 no ar e verdes.

**P6.0** — `select system_prompt from bot_settings where id=1;` → salvar em `prompt_backup_<data>.txt` (é o único rollback).
**P6.1 — F5, linha 51 (afirmação FALSA).** Hoje diz que a busca coloca bestsellers no topo — medido: 8 de 14 pares não tinham nenhum bestseller no top-10, e o boost rodava depois da RPC. Novo texto, já com **regra de PREFERÊNCIA** `[AJUSTADO]`: *"A `consultar_estoque_supabase` devolve no máximo 8 produtos DISTINTOS, já ordenados: os primeiros são os mais parecidos com o que o cliente pediu e os seguintes podem ser campeões de venda **da mesma categoria**, marcados com `destaque: true`. Ainda assim a lista pode conter item de categoria vizinha: leia o nome de CADA produto e descarte o que não for a categoria pedida antes de montar `produtos_recomendados`. Nunca inclua item de outra categoria para completar 3. **Entre os produtos da categoria certa, prefira os marcados com `destaque: true`.** Quando um item tem `destaque: true` você pode dizer que é um dos mais vendidos da loja — nunca invente esse selo para os outros."*
**P6.2 — F2, verdade da tool.** *"O campo `filtro_aplicado` do retorno da ferramenta é a VERDADE sobre o que foi consultado — acima da sua memória do que você pediu. Nunca afirme nada sobre um tamanho que não esteja em `filtro_aplicado`. Cada item traz seu próprio campo `tamanho`, que pode ser composto ('P/M','G/GG') ou 'ÚNICO': **não diga que a peça é do tamanho X** — se o tamanho for composto, diga que serve nos dois."* `[BLOQUEADO parcial: linguagem exata do composto — ver §9.2]`
**P6.3 — F3, fotos** (`prompt_frente3_fotos.md`): §0 (URL nunca aparece), §2 passo 5 (`n_fotos` maior = mais ângulos), §5 (bloco `FOTOS:` reescrito), §15 (4→5 motivos nas **linhas 285, 295 e 303**, com `pedido_foto`). **Pré-requisito operacional: `FOTOS_SOB_DEMANDA=1` no Railway** — com o switch em 0 o prompt mandaria chamar tool inexistente. `[BLOQUEADO: texto da mensagem de handoff — §9.3]`
**P6.4 — F5/P2, variedade.** *"Se o cliente pedir 'tem mais?', mostre produtos DIFERENTES dos que você já enviou nesta conversa. A ferramenta já exclui automaticamente os últimos que você mostrou; se ainda assim vier o mesmo, diga que essas são as opções da categoria e ofereça outra categoria ou atendente — não repita os mesmos cards."*
**P6.5** — Deploy, `run_eval.py` completo, **novo baseline**, e `dump` do prompt do banco de volta em `prompt_luna_v2.txt` (a cópia local está defasada — é isso que gera confusão em cada rodada).

---

## 9. RISCOS E ROLLBACK

| Frente | Rollback | Sintoma que justifica reverter | Nota |
|---|---|---|---|
| **F1** | Puro git, ordem inversa (`revert` C1.4→C1.1). **Parcial é possível e preferido**: C1.3 e C1.2 são independentes. **C1.1 só como bloco** — reverter parte dele deixa o buffer **sem nenhum pop**, vazando para sempre | `espera_lock=` acima de ~60s (fila de turnos) ou cliente reclamando de resposta em rajada atrasada | Zero DDL. Rows gravadas durante a vigência são válidas nos dois esquemas |
| **F2** | Commits **1→2→3 encadeados**: revert só na ordem inversa (2 e 3 consomem `_tokens_tamanho`/`tokens_alvo`/`dropados` do 1; reverter o 1 isolado dá `NameError` e derruba toda a busca) `[AJUSTADO]`. Commits 4 e 5 independentes. Banco: `rollback_0011` (recria a 3-args com o corpo exato + `NOTIFY`) e `rollback_0010` (`DROP TABLE`) | Modelo lendo o `aviso` em voz alta ao cliente; recall alto demais gerando reclamação de tamanho | Derrubar `tool_filtro_eventos` **não** quebra o bot (insert em try/except) |
| **F3** | **Nível 1 sem redeploy:** `FOTOS_SOB_DEMANDA=0` + restart → tool sai da declaration e o sender vira no-op; ordem determinística, `n_fotos` e foto em `consultar_produto_por_id` continuam (inofensivos). **Nível 2:** revert dos 2 commits de código | Perda recorrente de foto no log (Meta devolvendo erro), ou cliente pedindo cor que não existe | Sem DDL. `_fotos_vistas` é memória: restart zera (pior caso 1 foto repetida) |
| **F4** | Código: `git revert` (pior caso volta a apagar vetor, que o re-embed depois recupera). Migration 0012: `drop function embeddings_health()` (o `/health` degrada devolvendo `embeddings: {}`). Alarme: apagar `ALERT_*` ou subir `EMB_NULL_ALERT_MAX`. **Re-embed total (única etapa destrutiva):** `update produtos_estoque p set embedding = b.embedding from embedding_backup_20260808 b where b.id_unico = p.id_unico;` — **atenção: devolve TAMBÉM os vetores contaminados e os NULLs**, é volta ao estado ruim conhecido, não a um estado neutro | Ciclo do sync passando de ~7 min (baixar `SYNC_REEMBED_MAX` por env, sem redeploy) | Se o re-embed morrer no meio, **NÃO restaurar**: retomar com `--skip=<último i>` (idempotente por construção) |
| **F5** | **Ordem obrigatória: código primeiro, banco depois.** Invertido, o bot chama `limite_produtos`/`excluir_ids` numa função que não os tem e **toda busca falha**. Banco: `rollback_0013` + `NOTIFY`. O índice `idx_produtos_estoque_id_produto` **fica** | Cenário de curadoria caindo (ajustar o escopo lexical, não o assert); "não achei nada" subindo (checar o fallback de exclusões) | Nenhuma escrita em `produtos_estoque` — só criação de índice |
| **F6** | `inserir_prompt.py` com `prompt_backup_<data>.txt` | Qualquer regressão de conformidade na eval pós-deploy | Único artefato: 1 UPDATE em `bot_settings` |

### Se a suíte quebrar
1. **Reexecutar só o cenário** (`--only <id>`) — checks de juiz têm variância.
2. **Comparar com o baseline correto**, não com o CSV de 14/07 (3 commits atrás; e após a F6, o baseline é o novo).
3. **Suspeito nº1 por frente:** F1 → harness (`run_eval.py:512` seta `message_buffers` e o drain o consome; verificado compatível) ou o unpack `tamanho_alvo, _ =`; F2 → truncagem do digest / `params` como string JSON; F3 → chave `pid` int vs str; F4 → cenário que dependia da ordem antiga de similaridade **ou índice HNSW degradado** (rodar o check de recall antes de culpar o texto canônico); F5 → escopo lexical folgado.
4. **Nunca** relaxar o assert nem "compensar no prompt": o knob é sempre o parâmetro que causou.

---

## 10. DECISÕES TRAVADAS NO CLIENTE

### 10.1 Podem andar SEM ele (comece já)
**F1 inteira** · **F2 inteira** (a pergunta sobre RLS é confirmação de padrão já vigente — sigo o padrão do projeto: RLS OFF + grant a anon, porque a chave do bot é anon e todas as tabelas atuais são assim) · **F4 inteira** (a janela do re-embed deixou de existir: bot desligado) · **F5 código+DDL** com os defaults `minimo_grade=3` e "campeões de venda primeiro, profundidade só desempata" · **F3 código** (o teto de 4 fotos é env-tunável, então mudar de opinião depois é variável de ambiente, não código).

### 10.2 BLOQUEADO — precisa de resposta antes de **F6 (prompt)**
1. **Tamanho COMPOSTO** ('P/M' 20 linhas, 'G/GG' 18, com estoque): depois do overlap a peça 'P/M' aparece numa busca por M — fisicamente correto, mas o card **não mostra o tamanho** nesta rodada. (a) aceitar como está; (b) aceitar e a Luna avisar em texto *"essa é P/M, serve nos dois"*; (c) continuar escondendo. → **muda o texto do P6.2**.
2. **Motivo `pedido_foto`**: quando cair para a atendente, ela vai **tirar foto nova** da peça, ou só descrever/mandar vídeo? → **muda a mensagem-padrão do §15**.
3. **Silêncio pós-handoff / bot desligado**: mensagens do cliente passam a ser **descartadas** (continuam em `chat_history` para a atendente ler, mas a Luna nunca responde). Hoje o comportamento acidental é acumular e responder a um texto que junta **até 8,9 dias**. Confirma descartar, ou quando a atendente devolve a conversa a Luna deve responder à **última** mensagem? → o plano implementa **descartar** (é o correto); a alternativa exige código extra.
4. **Mensagem nova durante resposta em voo**: o desenho responde às duas, **em ordem** (nunca os mesmos cards de novo). A alternativa é engolir a nova quando a resposta que saiu já a contempla. → o plano implementa "sempre responder, só em ordem".

### 10.2-bis DECISÃO NOVA, descoberta em 09/08 — **tamanho ÚNICO desaparece de toda busca com tamanho**

Achado ao investigar a falha crônica de `fantasia`, e é a causa raiz de verdade — mais fundo que "só tem 1 fantasia em estoque".

O filtro de tamanho da RPC casa por overlap de tokens: `_tokens_tamanho('ÚNICO')` → `['UNICO']`, que **nunca** faz overlap com `['M']`. Então **qualquer** busca em que o cliente diz um tamanho exclui **todas** as peças de tamanho único:

| Recorte | Linhas | Produtos |
|---|---|---|
| Tamanho ÚNICO no catálogo | **1.154** (17,1% de 6.739) | — |
| … com estoque > 0 | 674 | **404** |
| … com estoque, na Matriz (a única loja que o prompt permite) | 166 | **166** |

Ou seja: **166 produtos com estoque na Matriz ficam invisíveis** quando o cliente menciona tamanho. É a mesma classe do bug de `embedding` NULL que corrigimos hoje — o cliente pede, a Luna diz que não tem — só que por caminho diferente. Foi exatamente isso que produziu a falha de `fantasia`: a única fantasia da Matriz é ÚNICO, então "fantasia tamanho M" devolvia 8 itens e **zero** fantasias, e o modelo completava com SEX SHOP.

**A decisão:** peça de tamanho único deve aparecer para quem pediu M/G/GG?
- **(a) Sim** — é o mesmo formato da decisão (b) que o dono já tomou para `P/M`: a peça aparece e a Luna avisa o tamanho real ("essa é tamanho único"). A regra de aviso **já está no prompt** (§6), então o custo é só o overlap no filtro. Consistente com a decisão já tomada.
- **(b) Não** — mantém como está e os 166 produtos seguem invisíveis em busca com tamanho.
- **(c) Sim, mas só quando a busca sem ÚNICO vier vazia** — fallback; mais código, menos previsível.

**Não implementado de propósito:** muda o resultado de **toda** busca com tamanho (não só fantasia), então precisa da decisão do dono + janela de gate própria. A mitigação que ENTROU nesta rodada é a `instrucao_curadoria` dinâmica: quando nenhum item da lista é da categoria, a Luna não recomenda nenhum e é honesta — medido, o modelo passou a **buscar de novo sem o filtro de tamanho** e achar a peça ÚNICO por conta própria.

### 10.3 BLOQUEADO — calibração comercial (não trava código; muda só um número/env)
5. **`minimo_grade`**: 3, 5 ou 10 peças somadas para um campeão de venda ser promovido? (3 é escolha minha, não medida.)
6. **Atacado**: revendedor vê primeiro os **campeões de venda** (default entregue) ou os de **grade mais funda**?
7. **Teto de fotos por pedido**: 4 por produto por turno cobre integralmente 191 dos 208 produtos com 2+ fotos; os 7 com 7-10 fotos precisam de 2-3 pedidos. Manter, ou mandar tudo de uma vez (até 9 imagens seguidas)?
8. **Fotos de outras cores** (achado novo, verificado olhando as imagens): no produto 8060563 (10 fotos) as fotos 1-2 são frente e costas do **vermelho** e a foto 3 é o modelo **preto**; nos produtos com 2 fotos (caso dominante, 142 com estoque) são frente+costas da mesma cor. Não há dado de cor no banco e o §7 exige atendente para confirmar cor. Mandar todas (aceitando *"quero a preta"*) ou limitar às 2 primeiras?
9. **Loja 112102**: ERP hoje a chama **'LIMP +'** (limpeza), o banco guarda 1.318 linhas rotuladas **'SAO MATEUS'** (nome que não existe mais), 538 com estoque>0 — e esse nome chega ao modelo (bot.py:507). (i) manter e propagar o nome do ERP (fix de 1 linha: a detecção de mudança em sync_erp.py:348-356 não compara `loja`); (ii) excluir a loja do sync; (iii) zerar o estoque.
10. **Grupo do ERP**: os grupos do GestaoClick são **mutuamente exclusivos** — um baby doll campeão de venda **perde** o grupo de categoria (verificado: os 125 produtos do grupo campeão não aparecem em nenhum outro com estoque). Isso me obriga a inferir categoria pelo **nome**. Dá para marcar "mais vendido" de outra forma (tag, campo extra, segunda lista) preservando o grupo de categoria? Isso tornaria o escopo da F5 **exato** em vez de heurístico.
11. **Alarme de produtos invisíveis**: limiar 20 variações com estoque sem vetor, reenvio a cada 12h, WhatsApp+e-mail. Quem recebe — o dono da loja também, ou só o time técnico? (A mensagem é comercial: *"o cliente não encontra esses produtos"*.)
12. **Nº de opções**: a busca passa a devolver 8 produtos distintos, mas o prompt limita a 3 recomendações e o código a 5 cards. O cliente deveria poder ver mais de 3 por vez?

---

## 11. BACKLOG EXPLÍCITO (fora desta rodada, com o motivo)

| # | Item | Por que fica fora |
|---|---|---|
| B1 | **Contrato "uma linha por produto com array de tamanhos"** | **Teto de escopo declarado.** Quebraria `extrair_produtos_de_tool_results`, o cache de cards e a suíte inteira. A F5 escolhe uma linha *representativa* mantendo o shape atual |
| B2 | **Imprimir tamanho no card** | Restrição dura: muda a legenda → muda `card_envios.legenda` → afeta o reply-to-card. É a defesa em profundidade natural para 'P/M' e para o P3, mas exige rodada própria (com migração de leitura de legenda) |
| B3 | **Vocabulário de `_tamanhos_validos_na_msg`**: EG (46 linhas com estoque), 2EG (7), XL (4), G4 (3) | Muda **quando** o guard dispara (mais correções, novo risco de falso-positivo) e **não** é causa do falso "não tenho" — esses valores são token único e passavam até na igualdade exata. `tool_filtro_eventos` (F2) é justamente o instrumento para medir a lacuna antes de mexer |
| B4 | **Coluna `ordem`/`tipo`/`principal` em `produtos_imagens`** | `sync_images.py:119` faz DELETE+INSERT: o `id` **não** é estável entre syncs, e `created_at` é idêntico em todas as rows. Ordem tem de ser **gravada** pelo sync, não inferida — projeto de dado |
| B5 | **Gênero/público do produto** | Dos 21 pijamas da Matriz só 1 é explicitamente masculino (só no M). A conversa que terminou em "Desisto" foi por **gênero**, não por repetição. Não há dado; exige cadastro |
| B6 | **Itens de venda no GestaoClick** | `vendas` é **só cabeçalho**, sem `id_produto`. Sem isso não existe ranking por venda real — o grupo curado do ERP é o único sinal, e estoque é só profundidade |
| B7 | **Limpar as linhas envenenadas em `chat_history`** | 67 usuários têm rows `user` com dias de texto concatenado, e `get_history(limit=30)` vai continuar servindo esses monstros ao Gemini. Decisão de dado (apagar as rows-prefixo vs truncar `content` no contexto) — frente própria. **É o que torna o filtro de data no SQL de fechamento da F1 obrigatório** |
| B8 | **Redis para buffer/lock compartilhado** | Só se e quando a precondição de réplica única (Gate 0.1) deixar de valer |
| B9 | **Higiene de memória**: `_user_locks`, `_user_turn_locks`, `_rate_limit_history`, `_atacado_users`, `_fotos_vistas`, `_mostrados` crescem sem limite (um entry por cliente, para sempre) | Vazamento lento, irrelevante no volume atual. `_fotos_vistas` e `_mostrados` já nascem com trim/TTL |
| B10 | **Idempotência genérica de tools no retry de 3 tentativas** | Auditado: a única tool com efeito externo é `transferir_para_atendente`, já idempotente pela janela de 2h (bot.py:968-973); `registrar_card_enviado` roda uma vez só, no render, fora do laço; `mostrar_fotos_produto` é read-only por construção; `_log_filtro_evento` duplica de propósito (é log). **Qualquer tool NOVA com efeito colateral precisa de guarda própria antes de entrar** |
| B11 | **`tamanho_tokens` no `RETURNS TABLE` da RPC** | Seria a fonte única de verdade, mas exigiria DROP+CREATE da função viva e engorda o payload. Desnecessário: o Python espelha com teste de contrato (F2) |
| B12 | **Dropar o índice HNSW** | A RPC da F5 não o usa (KNN exato de propósito), mas ele fica: custa só tempo de escrita e volta a ser necessário quando a tabela crescer — basta trocar os dois `SET` por `SET hnsw.iterative_scan='relaxed_order'` (medido: devolve o pool cheio em 2-52 ms) |
| B13 | **Normalizar espaço interno no texto do embedding** (103 linhas com espaço duplo) | Invalidaria **todo** vetor já escrito pelo sync em troca de ganho nulo. `.strip()` é seguro porque 0 linhas têm espaço em borda |
| B14 | **Retenção/expurgo de `tool_filtro_eventos`** | Guarda termo buscado e user_id. Se o dono quiser, política de 90 dias + RLS com policy de insert para anon (decidir junto, não improvisar) |
| B15 | **`{percentual}` e `{total/6}` não existem no retorno de `calcular_total`** — achado da revisão adversarial da F6 (09/08). O retorno real é `{status, modo, primeira_compra, subtotal_varejo, total, desconto, minimo_exigido, minimo_atingido, falta_para_minimo, parcelado, itens_detalhados}` (bot.py:1372-1383). O template **obrigatório** de atacado usa `🎁 VOCÊ ESTÁ ECONOMIZANDO: R$ {desconto} ({percentual}% de desconto!)`, e as TÉCNICAS 1 e 6 usam `R$ {total/6}` — ou seja, o prompt manda o modelo **calcular de cabeça** exatamente onde diz "VOCÊ NÃO CALCULA TOTAL DE CABEÇA. NUNCA". **Agravante:** `percentual` existe em OUTRA tool (`verificar_promocao_hoje`, bot.py:1399) — há colisão de nome, e o modelo pode trazer o percentual de uma promoção para o resumo de atacado. | Predata a F6 e muda comportamento de **atacado** (que tem 4 cenários próprios na suíte). Mexer aqui na mesma janela do prompt violaria a §1 da `POLITICA_DE_GATE.md`. Correção provável: `calcular_total` devolver `percentual_desconto` e `parcela_6x` calculados no Python (fonte única), e o prompt citar os campos novos |
| B16 | **Docstring de `calcular_total` anuncia `parcelado_6x`; o código devolve `parcelado`** | Mesma revisão. Uma linha, mas é contrato de tool: entra junto com B15 para não gastar duas janelas de gate |
| B18 | **`produtos_recomendados` com id sem busca no turno** — `recommended_empty_when_no_search` falhou nos **dois** gates completos, com ids diferentes (`[0]` no #2, `[14681170]` no #3). O modelo põe id no JSON em turno que não chamou busca | Inócuo em produção: `renderizar_mensagem_estruturada` ignora id fora do cache do turno, então nenhum card errado sai. É desobediência de contrato, `media`, e some se o §0 ganhar uma linha — mas §0 é o bloco mais lido do prompt e mexer nele sem gate próprio é risco desproporcional ao dano |
| B17 | **A falha crônica de `fantasia` é ESTOQUE, não prompt** — medido em 09/08: a Matriz (`id_loja=244033`, a única que o prompt permite) tem **1 linha** de fantasia com estoque: `40214981 FANTASIA LUXO DIVERSAS`, tamanho ÚNICO, **1 unidade**. Existem 11 linhas com "FANTASIA" no nome no catálogo, 10 delas em **outras lojas**. Ou seja: pedimos ao modelo até 3 fantasias e existe 1 unidade de 1 fantasia — toda falha é ele completando a lista com vizinhos. **A/B com 3 repetições por braço** provou que não é regressão da F6: `curadoria-fantasia-off-category` falha 1/3 no prompt antigo e 2/3 no novo, **com os ids idênticos** (34176431 SEX SHOP TESÃO DE TOURO, 44094069 SEX SHOP VIBRADOR) nos dois — n=3 não distingue 1/3 de 2/3 | Não se corrige com texto. Três caminhos, todos fora desta rodada: (i) **dado** — a loja repor fantasia; (ii) **código** — instrução dinâmica no payload no molde do §3.9 ("só 1 item desta lista é da categoria; recomende só ele"), que é o mesmo padrão que resolveu o tamanho composto 3/3; (iii) **teste** — o cenário aceitar 1 card como sucesso em vez de esperar 3. A (ii) é a de melhor retorno e tem precedente medido |