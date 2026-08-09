# Política de gate — como rodar a suíte sem quebrar o cliente nem a conta

Escrito em 09/08/2026, depois de a conta do Gemini bater o **teto de gastos mensal** durante a rodada de correção dos 4 defeitos. Não foi o bot que gastou: foi a verificação. Este documento existe para isso não se repetir.

---

## 1. Os números que justificam a política

Tudo medido, não estimado:

| Fato | Valor |
|---|---|
| System prompt (`bot_settings.system_prompt`, id=1) | **7.460 tokens**, reenviados em **toda** chamada |
| Entrada média por turno de produção | **10.957 tokens** |
| Saída média por turno | **80 tokens** (a entrada é **137×** a saída) |
| Produção inteira, 19/jul → 07/ago (45 turnos) | **449 mil** tokens de entrada |
| **Uma** execução da suíte completa (23 cenários / 35 turnos) | **~1 milhão** de tokens |

**Uma rodada da suíte custa mais que dois meses de operação real.** No dia do incidente a suíte completa rodou 5 vezes, mais 10 execuções de A/B, mais 5 agentes rodando os próprios subconjuntos.

Dois multiplicadores silenciosos:

- **Function calling reenvia tudo.** Cada ida-e-volta de tool remanda o contexto inteiro. Medido: o modelo chamou a mesma tool **3 vezes num turno** → 4 requisições para **uma** mensagem de cliente.
- **O retry triplica.** Cada tentativa após 504 remanda o contexto completo.

### Cache: o que já existe e o que não vale a pena

O **cache implícito do Gemini já está ativo** e cobre ~55% do prompt de graça:

```
call 1: prompt=7466 | cached_implicito=4081
call 2: prompt=7466 | cached_implicito=4081
```

O **cache explícito** (`google.generativeai.caching`) foi testado e funciona com `gemini-3-flash-preview`, levando a 99,9% (`prompt=7467 | cached=7460`). **Não está implementado de propósito**: cobra armazenamento por hora e só compensa sob tráfego denso. Com 45 turnos em três semanas, manter o cache vivo custa mais do que economiza. **Revisitar quando o volume crescer** — se o bot passar a atender de verdade, vira ganho claro.

Tirar o juiz do Gemini foi avaliado e **descartado**: o prompt de juiz típico tem ~156 tokens, ou seja ~1% do custo. Não é alavanca.

---

## 2. As regras

### 2.1 Durante o desenvolvimento de uma frente — nunca a suíte inteira

```bash
.venv/Scripts/python.exe tests/eval/run_eval.py --dimension tamanho
.venv/Scripts/python.exe tests/eval/run_eval.py --only <id-do-cenario>
.venv/Scripts/python.exe tests/eval/run_eval.py --only <id> --no-judge
```

Rode só a dimensão que a sua mudança toca. Dimensões: `anti-injecao`, `atacado-identidade`, `curadoria`, `degradacao`, `foto`, `handoff`, `happy-path`, `preco`, `tamanho`.

### 2.2 Suíte completa — só em marco

Vale rodar inteira ao fechar uma frente e antes de deploy. Não vale rodar "para conferir".

```bash
.venv/Scripts/python.exe -u tests/eval/run_eval.py 2>&1 | tee /tmp/gate.log
```

Use `-u` e `tee`: sem isso o pipe bufferiza e você fica sem progresso por 12 minutos (aconteceu).

### 2.3 Nunca rodar a suíte em paralelo com um agente de implementação

Os dois batem no mesmo Gemini. Medido no dia do incidente: um gate rodando junto com um agente produziu **105 erros de 504 num único run**, **34 skips** e terminou em 290s em vez de 709s. O resultado foi **inválido** — mediu a indisponibilidade da API, não o código.

### 2.4 Sondar o Gemini antes de julgar qualquer gate

```bash
.venv/Scripts/python.exe /tmp/probe_gemini.py   # 3 chamadas triviais
```

Assinatura de run envenenado por infra, que **não** é falha de código:

- `lat=0ms`, `fmt=error`, `tools=[]`
- muitos `skipped` (o normal é 0-1)
- tempo total muito abaixo do de referência
- `429 ... exceeded its monthly spending cap` → **teto de gastos**, não é transitório

### 2.5 Um run não é evidência — LLM no meio exige A/B

Cenário que falhar **não** é regressão até ser medido nos dois códigos:

```bash
git worktree add /tmp/wt_antes <commit-anterior>
cp .env /tmp/wt_antes/.env                       # .env e tests/eval sao gitignored
# rodar o mesmo --only N vezes nos dois lados e comparar
git worktree remove --force /tmp/wt_antes
```

**Caso documentado:** `curadoria-fora-categoria-fantasia` (check **grave** `recommended_only_in_category`) falha **5/5 tanto no código novo quanto no antigo**. Havia passado por sorte na única medição do baseline, criando a ilusão de "zero graves". Não é regressão de ninguém — é instabilidade crônica de curadoria, e o conserto é de prompt (F6).

### 2.6 Critério de aprovação

**Zero falha GRAVE nova em relação ao baseline verificado por A/B.** Não é "100%": a suíte tem cenários instáveis e checks de juiz com variância.

Baseline de referência (09/08/2026, HEAD `c7ee4dc`, pós-F2):

```
CHECKS: 70 | pass=69 fail=1 skipped=0 | taxa=99%
graves: 38/38 = 100%
tempo: 709s
```

**Falhas conhecidas e esperadas** (não bloqueiam):

| Cenário | Check | Por quê |
|---|---|---|
| `tamanho-composto-pm-aparece-para-m` | `nao_nega_estoque_sem_base` (média) | A Luna chama `P/M` de `M`. Lado de código pronto; a linguagem depende da decisão §10.2 item 1. **Voltar para `grave` no deploy do prompt.** |
| `curadoria-fora-categoria-fantasia` | `recommended_only_in_category` (grave) | Instável nos dois códigos (ver 2.5). Escopo F6. |
| `foto-unica-esgotou-angulos` | `no_photo_false_promise` | Variance-prone; depende da curadoria do turno anterior. Escopo F6. |

---

## 3. A suíte escreve em PRODUÇÃO — cuidado ao matar o processo

`tests/eval` bate no **Supabase e no Gemini reais** (só o envio de WhatsApp é stubbed). O cleanup roda no fim e cobre `bot_turns`, `chat_history`, `conversation_handoffs` e `tool_filtro_eventos`.

**Matar o processo salta o cleanup.** Limpeza manual:

```sql
delete from conversation_handoffs where user_id like 'eval_%';
delete from bot_turns            where user_id like 'eval_%';
delete from chat_history         where user_id like 'eval_%';
delete from tool_filtro_eventos  where user_id like 'eval_%';
```

Todo SQL de métrica precisa filtrar `user_id not like 'eval_%' and user_id not like 'test_%'` — senão o tráfego de teste entra na conta.

**Atenção ao processo pendurado:** um run travou 11h44 sem gravar nada, porque a máquina dormiu e a chamada gRPC morreu sem estourar timeout. Confira antes de relançar:

```bash
# PowerShell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*run_eval*' }
```

O teto de tempo do turno (`TURNO_ORCAMENTO_S=150`) protege o **bot**, não o harness.

---

## 3.9 Regra genérica no prompt não muda comportamento; instrução DINÂMICA no `function_response` muda

Medido na F6 (09/08), decisão (b) do dono — *"a Luna avisa que a peça 'P/M' serve nos dois"*:

| Onde a regra estava | Resultado medido |
|---|---|
| §6 do prompt ("tamanho composto → diga que serve nos dois") | **falhou** |
| `filtro_aplicado.observacao_tamanho`, texto **estático** em todo payload | **falhou** |
| `filtro_aplicado.instrucao_tamanho_composto`, **dinâmica e nomeando as peças** | **3/3 passou** |

A resposta real com as duas primeiras: *"opções sem costura no M"* — o item `P/M` recomendado e o composto nunca mencionado. Com a terceira: *"a Calcinha Sem Costura Fio e a Calcinha Alta Fio são P/M e servem nos dois tamanhos 💕"*, nas três repetições.

**A lição operacional:** instrução que vale para *toda* chamada vira ruído de fundo; o modelo a trata como preâmbulo. Instrução que aparece **só quando o caso ocorre** e que **nomeia os itens concretos** entra no `function_response` — o trecho mais recente do contexto — e é obedecida. Bônus: custo de token zero quando o caso não ocorre (as 38 linhas compostas do catálogo contra 6.739 no total).

**Aplicar este padrão** antes de escrever mais parágrafo no prompt: se a regra depende de uma condição que o código sabe detectar, a regra pertence ao payload, não ao prompt.

## 4. Pendências desta política — NÃO ESQUECER

Levantadas junto com este documento e ainda **não implementadas**:

### 4.1 Contabilidade de token no `run_eval.py` — prioridade alta

Hoje a saída da suíte **não mostra custo nenhum**. Foi exatamente por isso que o teto estourou sem aviso: o gasto era invisível.

Implementar: acumular `usage_metadata` (`prompt_token_count`, `candidates_token_count`, `cached_content_token_count`) de cada turno e de cada chamada de juiz, e imprimir no rodapé do run, junto do CSV. Com isso "a suíte custa ~1M tokens" vira número medido a cada execução em vez de estimativa.

### 4.2 Suporte a `GEMINI_API_KEY_TEST` — prioridade alta

Hoje o harness usa a **mesma chave da produção** (confirmado: mesmo `sha256`). Ou seja, a verificação pode — e quase conseguiu — **derrubar o bot do cliente** ao estourar a quota.

Implementar: o harness lê `GEMINI_API_KEY_TEST` e, quando existir, sobrescreve `GEMINI_API_KEY` no ambiente **antes de importar `bot`** (o `genai.configure` acontece no import). Fallback para a chave atual quando a variável não existir, para não quebrar quem não tem a segunda chave.

### 4.3 Encolher o system prompt — AGORA MAIOR, não menor (medido em 09/08)

Era 7.460 tokens (24.265 chars). A F6 **subiu para ~8.806** (28.619 chars): **+1.340 tokens, +17,9%**, pagos em *toda* chamada de tool — e o `automatic function calling` reenvia o contexto inteiro a cada round-trip.

Foi uma escolha consciente, não um descuido: o texto novo é o que ativa as F1–F5 (selo `destaque`, `filtro_aplicado`, tamanho composto, tool de fotos, variedade). Prompt menor com as frentes desligadas é economia falsa. Mas a dívida ficou **maior**, e por isso o alvo agora está identificado:

**`medo_fraude` é explicado em QUATRO lugares** — §9 ESTRATÉGIA 3, §10.6 (roteiro de 4 passos), §15 caso 4 (linha da tabela) e §15.1 (protocolo detalhado). São ~2.400 chars (~740 tokens) descrevendo o mesmo comportamento. Consolidar em §15.1 com ponteiros nos outros três derruba de volta quase todo o crescimento da F6.

**Por que não foi feito neste deploy:** violaria a regra §1 desta política — duas mudanças na mesma janela e não se sabe qual causou o delta na eval. Consolidar `medo_fraude` é mexer em comportamento (o roteiro de fraude tem cenário próprio na suíte), então merece a sua própria rodada de gate, com o baseline da F6 já estabelecido como referência.

### 4.4 `google.generativeai` está descontinuado

O SDK emite `FutureWarning`: *"All support for the google.generativeai package has ended"*. Migrar para `google.genai`. Não é urgente, não estava no plano dos 4 defeitos, e mexe no caminho crítico — merece frente própria.
