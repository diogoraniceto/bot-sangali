# Suite de Conformidade da Luna (Sangali) — tests/eval

Avaliacao comportamental multi-turno da Luna contra a infra REAL (Supabase + Gemini).
As duas funcoes de envio do WhatsApp sao stubadas: as mensagens sao CAPTURADAS, nunca
enviadas. `ENABLE_INPROCESS_SYNC=0` e setado antes de importar `bot`, desligando o
scheduler/watchdog (que enviaria WhatsApp/cron).

## Como rodar

    python tests/eval/run_eval.py                     # todos os 18 cenarios
    python tests/eval/run_eval.py --dimension preco,foto
    python tests/eval/run_eval.py --origin auditoria-07-07,cobertura
    python tests/eval/run_eval.py --only atacado-identidade-card-id-6743024
    python tests/eval/run_eval.py --no-judge          # so checagens deterministicas
    python tests/eval/run_eval.py --no-cleanup        # mantem as rows de teste no banco
    python tests/eval/run_eval.py --judge-model gemini-3-flash-preview

Requisitos: `.env` na raiz do projeto (mesmas credenciais do bot). O juiz [JUDGE]
reusa o `google.generativeai` ja configurado no import do `bot`; default
`gemini-3-flash-preview` (sobrescreve com `EVAL_JUDGE_MODEL` ou `--judge-model`).

Cada assert vira uma linha no CSV `tests/eval/eval_results_<timestamp>.csv` com
`status` = pass | fail | skipped. Um resumo por dimensao e por severidade e impresso
ao final, mais a lista de FALHAS GRAVES. Por padrao o runner limpa as rows de teste
(`bot_turns`, `chat_history`, `conversation_handoffs`) do uid `eval_*` ao terminar.

## Como funciona

Cada cenario e uma conversa (`messages[]`, uma fala por turno). O runner roda cada
turno via `bot.process_and_respond(uid)`, acumula tudo que a Luna enviou/chamou ao
longo do cenario e avalia os `asserts` sobre esse contexto agregado. Cenarios de
"responder ao card" embutem no texto o preambulo sintetico do webhook fixando
`id_produto=N` (o harness chama `process_and_respond` direto, entao a injecao de id
que normalmente ocorre no webhook precisa vir no texto).

Checagens deterministicas (`sent_at_least_one_message`, `no_price_in_free_text`,
`recommended_only_in_category`, `recommended_empty_when_no_search`,
`calcular_total_uses_ids`, `handoff_triggered`) leem `bot_turns` + banco. As demais,
marcadas [JUDGE], usam um LLM juiz. Qualquer checagem sem dado suficiente vira
`skipped` (nunca falso-fail).

## Como adicionar um cenario

Os cenarios vivem em **`tests/eval/scenarios.json`** (fonte unica; `build_scenarios()`
apenas carrega esse arquivo). Acrescente um objeto ao array:

    {
      "id": "slug-kebab-unico",
      "dimension": "preco",              // curadoria|foto|anti-injecao|degradacao|preco|
                                         // happy-path|tamanho|handoff|atacado-identidade
      "origin": "cobertura",             // auditoria-06-07|auditoria-07-07|cobertura
      "description": "o que o cenario protege",
      "messages": ["fala do turno 1", "fala do turno 2"],
      "asserts": [
        {"checkType": "sent_at_least_one_message", "params": "{}", "severity": "grave"}
      ]
    }

Use SOMENTE checkType presente no dict `CHECKS` (no `run_eval.py`). **`params` e uma
string JSON** (o loader converte para dict), ex. `"{\"categoria\": \"fantasia\"}"` ou
`"{\"ids\": [6743024]}"` ou `"{}"`. Para responder a um card, inclua no texto da
mensagem o preambulo `[o cliente respondeu ao card do produto id_produto=N ...]`
(o harness chama `process_and_respond` direto, sem passar pelo webhook).

Nota: `EVAL_JUDGE_MODEL` / `--judge-model` deve ser um modelo que a chave Gemini do
`.env` tenha acesso (o bot usa `gemini-3-flash-preview`).
