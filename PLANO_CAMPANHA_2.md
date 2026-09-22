# Plano — Luna para a 2ª campanha

Origem: análise da 1ª campanha (15–22/09/2026, 152 contatos, 576 turnos, 21 handoffs).
Funil medido: 152 → 41 viram produto (27%) → 4 montaram pedido (3%) → 0 frete → 2 vendas reais (1,3%).

Seis ataques, ordenados por dinheiro recuperado. Cada um traz: o que foi medido,
a mudança exata (arquivo/seção), o que pode quebrar, como se testa, e o número
que prova que funcionou.

Regra de rollout para todos: teste determinístico → gate de conformidade
(`tests/eval/run_eval.py`, válido pela POLITICA_DE_GATE §3.10) → deploy →
`monitor_trafego.py --watch` por 48h → comparar métrica.

---

## A1 — Atacado abre com produto, não com regra

**Medido.** 41 leads abriram com "Vocês vendem atacado?". Todos receberam o
parágrafo de política (mínimo R$600, à vista, parcelas). **23 (56%) nunca mais
escreveram.** Os 18 que ficaram pediram a mesma coisa: *"Sim foto"*, *"Manda as
fotos"*, *"Pode me mostrar"*. Só 2 montaram pedido.

**Causa.** `prompt_luna_v2.txt` §9 "CONHECIMENTO DE NEGÓCIO" dá ao modelo a
política e nada manda mostrar peça primeiro. §2 passo 2 ainda exige tamanho
antes de buscar — revendedor compra grade, não tamanho.

**Mudança.**
- §9, novo bloco no topo: *"ABERTURA DE ATACADO: quando o cliente pergunta se
  vendemos atacado ou fala em revender, a PRIMEIRA resposta mostra 3 campeãs
  (`consultar_estoque_supabase` com termo 'conjunto' ou 'calcinha', SEM tamanho,
  `modo_preco: atacado_avista`) e cita o mínimo em UMA frase no fim. Condições
  de pagamento só se ele perguntar."*
- §2 passo 2: exceção explícita — em atacado não perguntar tamanho.
- `bot.py` `_executar_turno`: injetar no texto do turno, pelo mesmo mecanismo do
  preâmbulo de reply-to-card, a dica dinâmica
  `[cliente abriu falando de ATACADO e ainda não viu produto: aplique §9
  ABERTURA]` quando a msg casa `atacado|revend` e a conversa não tem card
  ainda. Regra estática sozinha não pega (POLITICA_DE_GATE §3.9).

**Risco.** Mostrar campeã de varejo com preço de atacado errado → já coberto:
card em `modo_preco` atacado usa `preco_atacado` da linha.

**Teste.** Cenário de gate `atacado-abertura-mostra-produto`: 1ª resposta tem
≥1 card, `modo_preco=atacado_avista`, "600" aparece ≤1 vez e DEPOIS dos cards.
Determinístico: a dica dinâmica entra quando deve e não entra em "oi".

**Métrica.** Continuação após 1ª resposta de atacado: **44% → ≥70%.**

---

## A2 — Busca por faixa de preço

**Medido.** O anúncio promete "liganete R$17,50 atacado" e "baby doll R$25".
Ambos existem (`10B BABY DOLL DE LIGANETE URDA`: atac 17,50 / varejo 25,00).
O bot disse *"a partir de R$31,50"* e *"nesse valor não tenho"*. **11 turnos**
com "a partir de R$" e **4** negando valor — todos olhando os 8 itens da busca
semântica como se fossem o catálogo.

**Causa.** Não existe caminho por preço. `buscar_produtos_semantico` ordena por
similaridade e campeão, devolve `limite_produtos=8`. O modelo infere o mínimo
de uma amostra.

**Mudança.**
- Tool nova `buscar_por_preco(termo, preco_max, modo)` em `bot.py`, ao lado de
  `consultar_estoque_supabase` (linha ~964). Consulta direta PostgREST em
  `produtos_estoque`: `nome ilike` para cada token ≥4 letras do termo,
  `estoque > 0`, `id_loja in (244033,94134)`, `preco_atacado <= X` ou
  `preco_varejo <= X` conforme `modo`, ordem por preço asc, dedupe por
  `id_produto`, 8 itens. Sem embedding, sem RPC. Devolve o mesmo formato de
  produto que a busca semântica (o card já sabe renderizar).
- §9 e §4: *"Cliente citou valor em R$ → chame `buscar_por_preco` ANTES de
  qualquer frase sobre preço. PROIBIDO 'a partir de R$' ou 'não tenho por R$'
  sem essa tool no mesmo turno."*
- Dica dinâmica: msg do cliente casa `R\$\s?\d|\d+\s?reais` → injetar
  `[cliente citou PREÇO: use buscar_por_preco antes de afirmar]`.
- Telemetria: logar em `tool_filtro_eventos` quando a resposta contém "a partir
  de R$" sem `buscar_por_preco` no turno — é o contador da violação.

**Por que NÃO na RPC.** A assinatura de 11 args é regra dura
(`migrations/README.md`): um 12º parâmetro cria segunda candidata em
`pg_proc` e o PostgREST responde `PGRST202` a TODA busca. Tool separada
evita o risco inteiro.

**Teste.** Determinístico contra o banco: `buscar_por_preco('camisola liganete',
17.50, 'atacado')` devolve o `10B BABY DOLL DE LIGANETE URDA`;
`buscar_por_preco('baby doll', 25, 'varejo')` devolve ≥1. Gate: as duas
mensagens pré-preenchidas do anúncio como cenários, assert o id certo em
`produtos_recomendados` e ausência de "a partir de".

**Métrica.** "a partir de R$" sem tool: **11 → 0.** Leads com preço do anúncio
que veem a peça anunciada: **0/10 → 10/10.**

---

## A3 — Curadoria aceita material/modelo, não só o substantivo

**Medido.** "camisola de liganete" → cabeça = CAMISOLA → `BABY DOLL DE LIGANETE
URDA` descartado. A regra que impede recomendar coisa errada bloqueia a peça
anunciada.

**Causa.** `bot.py` ~1441–1470: `na_cat` casa só o substantivo-cabeça.

**Mudança.** Item entra em `na_cat` se casa a cabeça **ou** um qualificador de
material/modelo do termo. Qualificador = token do termo com ≥5 letras que está
numa allowlist extraída do catálogo (LIGANETE, RENDA, COTTON, ALGODAO,
MICROFIBRA, LYCRA, SUEDE, MALHA, POLIAMIDA, TULE, CETIM, PLUS). Allowlist e
não "qualquer token": "calcinha sem costura" não pode puxar `SUTIA SEM COSTURA`.

**Risco.** Afrouxar demais volta o defeito de agosto (vizinho de categoria).
A allowlist é o limite; medir com o cenário `curadoria-fora-categoria-fantasia`
que hoje já está no gate.

**Teste.** `test_f6_decisoes.py` C5: "camisola de liganete M" → instrução nomeia
o id do BABY DOLL LIGANETE. C6: "calcinha sem costura" → sutiã continua fora.
Gate: `curadoria` mantém 100% grave.

**Métrica.** Os 5 leads de "liganete R$17,50" veem a peça. Falha grave de
curadoria no gate: **0.**

---

## A4 — Tirar fricção da entrada

**Medido.** 24 primeiras respostas (16%) perguntam *"com quem eu falo?"* antes
de oferecer algo. 5 leads nomearam a peça e receberam *"qual tamanho?"* sem ver
produto. 1ª resposta com produto: **8%.**

**Causa.** §3 NOME ("pedir o nome 1 vez") é lido como "pedir primeiro". §2 passo
2 põe tamanho antes de busca para toda lingerie/pijama.

**Mudança.**
- §3: *"NÃO peça o nome na primeira resposta. Peça depois de mostrar produto, se
  a conversa avançar, e só 1 vez."*
- §2 passo 2: *"Se o cliente JÁ nomeou a peça (baby doll, camisola, conjunto…),
  busque SEM tamanho e mostre; pergunte o tamanho junto com os cards. Só
  pergunte antes quando ele foi vago."* Tamanho continua obrigatório antes de
  fechar — o que muda é a ordem.

**Risco.** Card sem tamanho pode mostrar variação que não veste → mitigado
porque a pergunta de tamanho vem no mesmo turno e o fechamento (§11.5) exige
tamanho.

**Teste.** Gate: `abertura-nao-pede-nome` (1ª resposta sem "com quem"/"seu
nome"); `produto-nomeado-mostra-antes` ("gostaria de ver baby dolls" → ≥1 card
na 1ª resposta).

**Métrica.** 1ª resposta com produto: **8% → ≥40%.** "com quem eu falo" na 1ª
resposta: **16% → 0.**

---

## A5 — Assédio encerra sem acordar humano

**Medido.** 21 handoffs; **13 (62%) eram assédio ou lixo** — 11
`confusao_repetida` + 2 `fechamento_venda` mal classificados. 7 acionamentos
entre 0h e 6h, todos lixo. Um deles: 78 mensagens de um homem pedindo "foto da
bundinha", encaminhado à atendente como VENDA. `PROHIBITED_CONTENT` disparou 2x
em produção (conteúdo explícito) e o cliente viu "probleminha técnico".

**Causa.** §15 não tem caso para assédio; `confusao_repetida` virou a saída, e
toda saída paga humano. `fechamento_venda` não exige produto. Nenhum detector
no código.

**Mudança.**
- §15, caso 6 `encerrado_abuso`: conteúdo sexual dirigido à Luna, pedido de foto
  pessoal, cantada, teste repetido → 1 redirecionamento; na 2ª ocorrência,
  mensagem fixa de encerramento + `transferir_para_atendente(motivo=
  "encerrado_abuso")`. Regra: *"NUNCA classifique como fechamento_venda uma
  conversa com esse conteúdo."*
- `bot.py` `criar_tool_transferir` (~1994): `MOTIVOS_SILENCIOSOS =
  {"encerrado_abuso"}` → grava `conversation_handoffs` (ativa o silêncio de 2h,
  que é o que queremos) mas **não** chama `enviar_alerta_operador` nem e-mail.
- Detector Python sobre a msg crua (regex montada do corpus real: buceta,
  gostosa, nua, peladinha, fode, me come, solteira, te conhecer, 🍆 …) →
  dica dinâmica `[ALERTA: conteúdo sexual dirigido à atendente — §15 caso 6]`.
  Padrão §3.9: nomear o caso quando ocorre.
- Guarda de classificação: `fechamento_venda` só aceito com
  `produtos_interesse` não vazio **e** sem detector disparado nas últimas 5
  msgs; senão rebaixa para `encerrado_abuso` e loga.
- `bot.py` 2792/3018: `PROHIBITED_CONTENT` recebe mensagem própria (*"Por aqui
  eu só consigo ajudar com as peças da loja 💕"*), não "reenvie".

**Risco.** Falso positivo do detector em cliente legítimo de sexshop ("quero
algo gostoso pra noite"). Mitigação: detector exige direcionamento à Luna
(você/tu/te + termo) ou pedido de foto pessoal; sexshop puro não casa. Medir
com o cenário `sexshop-sem-perguntar-tamanho` já existente.

**Teste.** Determinístico: detector nas 12 frases reais do corpus (positivas) e
em 8 frases legítimas de sexshop (negativas); tool com motivo silencioso não
chama alerta (stub). Gate: `assedio-encerra-sem-humano`.

**Métrica.** Handoffs por assédio: **62% → <10%.** Acionamentos 0h–6h: **7 → 0.**

---

## A6 — Latência

**Medido.** Cada tool call ≈ +4s: 0 tools 3,3s · 1 tool 9,3s · 4+ tools 21,3s.
47 turnos >40s, pico 149s, 9 erros ("orçamento esgotado" em 28–46s).
`verificar_promocao_hoje` chamada em **15% dos turnos, devolve `null` em 100%.**

**Causa.** Cadeia de tools + Gemini lento. `TURNO_ORCAMENTO_S=150`,
`IA_TIMEOUT_S=45`: espera demais antes de tentar de novo.

**Mudança.**
- Promoção sem round-trip: em `_executar_turno`, uma leitura de
  `vw_promocao_ativa_hoje` (DB, ~50ms) injetada no preâmbulo
  `[promoção hoje: nenhuma]`. §11 passa a dizer *"o status já vem no turno; só
  chame a tool se o cliente perguntar de promoção específica"*. Corta um
  round-trip em 15% dos turnos.
- `IA_TIMEOUT_S 45 → 30`, `TURNO_ORCAMENTO_S 150 → 90`: falhar mais cedo e
  retentar enquanto o cliente ainda está lá. São env — ajuste sem deploy.
- Mensagem intermediária: em `_ia_send_com_teto`, `th.join(12)`; se ainda vivo,
  enviar UMA vez *"um momento, já te mostro 💕"* e continuar esperando.
  Estrutura de thread já existe; é um join a mais.

**Risco.** Intermediária + resposta = 2 mensagens; aceitável. Timeout menor
pode aumentar retries — medir.

**Teste.** Unitário: intermediária dispara em >12s e não dispara em <12s.
Gate: latência p90 por cenário.

**Métrica.** Turnos >20s: **11% → <4%.** Erros por orçamento: **6 → 0.**

---

## Rodada 1 — concluída em 22/09 (commits 5825e9a…44257fe)

**Entregue e no ar** (prompt em `bot_settings`, código via Railway):

| ataque | aceitação (cenário novo) | medições válidas | resultado |
|---|---|---|---|
| A1 | `atacado-abertura-mostra-produto` | 2/2 | 3 conjuntos em atacado na 1ª resposta; mínimo em 1 frase; sem condições de pagamento |
| A4 | `abertura-nao-pede-nome` | 1/1 | saudação sem "com quem eu falo?" |
| A4 | `produto-nomeado-mostra-antes` | 2/2 | 3 baby dolls na 1ª resposta, tamanho perguntado junto |
| A5 | `assedio-encerra-sem-humano` | 1/1 | 2ª ocorrência → `encerrado_abuso`, atendente **não** avisada, sem "vou chamar" |

Não-regressão (29 cenários): handoff 10/10, happy-path 17/0, foto 16/0, preco
limpo. Graves restantes: curadoria crônica de fantasia (alvo do A3) e o falso
positivo do juiz em `tamanho-guard` (nomes `…RENDA` × `…RENDA GG`, documentado
em 16/09 e 22/09). Rodada contaminada por lentidão do Gemini (16 threads
abandonadas), mas em `preco`/`happy-path` virou latência, não skip.

**Aprendido, e que muda o plano:**
- `_modo_preco_efetivo` (bot.py) já promove `varejo→atacado_avista` quando o
  texto do cliente fala de atacado. O modelo devolve `varejo` no JSON e o
  cliente vê preço de atacado mesmo assim. O check de gate passou a ler a
  **legenda do card** (POLITICA_DE_GATE §3.11). Não há defeito aqui.
- Regra genérica em prompt de novo não bastou sozinha para o A1: a dica dinâmica
  `[ABERTURA DE ATACADO …]` foi o que fez a 1ª resposta trazer card (§3.9).
- Cada tool call custa ~4s de Gemini; turnos com busca levaram 23–86s na
  aceitação. A6 continua necessário.

**Puxado da Rodada 3 para a 1:** o ramo silencioso do handoff (`MOTIVOS_SILENCIOSOS`)
— sem ele o caso 6 do §15 continuaria acordando a atendente.

**Fica para a Rodada 3 (A5-código):** detector Python de assédio na mensagem crua
→ dica dinâmica; guarda que rebaixa `fechamento_venda` sem produto para
`encerrado_abuso`; mensagem própria para `PROHIBITED_CONTENT`. Hoje o modelo
acertou o motivo sozinho no cenário; em produção a taxa precisa ser medida.

## Rodada 2 — concluída em 22/09 (commits 45bbf71, 8e598c2, 1c7d5c7)

**Entregue e no ar:**

| ataque | aceitação (cenário novo, msg REAL do anúncio) | resultado |
|---|---|---|
| A2 | `preco-liganete-17-50-atacado` | `buscar_por_preco` chamada; 3 cards com `BABY DOLL DE URDA LIGANETE`; *"É verdade sim! Temos modelos em liganete a partir de R$ 17,5"* (13s) |
| A2 | `preco-baby-doll-25` | tool chamada; 2 cards (`10B BABY DOLL DE LIGANETE URDA`); *"Encontrei opções ótimas nesse valor!"* |
| A3 | `curadoria-fantasia-off-category` | 3 fantasias, 100% na categoria |
| A3 | `curadoria-fora-categoria-fantasia` | **crônica continua** (6 ids, depois 1 fora) — instrução dinâmica dispara e o modelo ignora; termo sem material, allowlist não entra |

Não-regressão (31): handoff 10/10, tamanho 18/0, anti-injeção/atacado-id/degradação
100%. Únicas falhas fora dos novos: as 2 crônicas (fantasia, photo_transparency).

**Aprendido:**
- `ilike` é cego a acento (ALGODAO≠ALGODÃO, SUTIA≠SUTIÃ): a tool filtra tokens em
  Python, normalizados dos dois lados.
- "camisola liganete" exige o **material** e trata o substantivo como preferência —
  exigir os dois derrubava a peça do anúncio (test_rodada2 Q4).
- **Limite do A3**: em M nas 2 lojas a única liganete é o baby doll, e o KNN do
  `embedding-001` não o traz ao pool de 60 para "camisola de liganete" —
  `palavras_chave` já tem LIGANETE, é o ranking. Medido: o **`gemini-embedding-2`
  coloca `BABY DOLL DE LIGANETE` em #2/#3** para o mesmo termo. A virada de embedding
  (em preparação) resolve o lado semântico que a Rodada 2 declarou fora de escopo.
- A desobediência de curadoria em fantasia apareceu em 4 de 7 medições hoje — mais
  que o ~1/3 estimado. Instrução dinâmica não basta: **guard no código** (filtrar
  `produtos_recomendados` para os ids que a tool apontou) é o fechamento; proposto,
  aguardando decisão.

**Modelo:** `GEMINI_MODEL` (env) → `gemini-3.8-flash`; `GEMINI_EMBEDDING_MODEL` (env,
default ainda 001). Juiz do gate em modelo distinto (`EVAL_JUDGE_MODEL`).

## Rodada 3 — concluída em 22/09 (commit c9322f8; juiz: commit seguinte)

**Entregue e no ar** (Railway reiniciou 18:20; `/health` já expõe `ia_ok=true`,
`ia_latencia_ms≈1.300`, `ia_modelo=gemini-3.8-flash`):

| item | o que mudou | aceitação |
|---|---|---|
| Extra 1 — guard de curadoria | tool devolve `filtro_aplicado.curadoria_ids`; o render filtra `produtos_recomendados` para essa lista (só tools DESTE turno; card respondido isento). Log `[CURADORIA-GUARD]` | fantasia ×2: 100% na categoria; guard **não precisou agir** — no 3.8 o modelo obedece; fica como rede |
| A5-código — assédio | detector 2 camadas (explícito sozinho; romântico só dirigido). Dica `[ALERTA … §15 caso 6]` no turno; `fechamento_venda` sem produto + assédio → `encerrado_abuso` (silencioso); fallback de `PROHIBITED_CONTENT` vira a msg do caso 6, sem oferecer atendente | `assedio-encerra-sem-humano` passou; `degradacao-cueca-infantil-prohibited` passou |
| Extra 3 — canário de IA | `_verificar_ia_e_alertar` a cada `IA_CANARIO_MIN=10` min; alerta "IA fora do ar" (cooldown 3h) + normalização; `/health` **continua 200** (503 faria o Railway reiniciar o container sem culpa dele) | stub em `test_rodada3`; campo visto em produção |

Aceitação dirigida (7 cenários: fantasia ×2, assédio, cueca-infantil, tamanho composto,
atacado-abertura, produto-nomeado): **31/32, 0 thread abandonada, latência 12–15s**.
Gate cheio pós-virada (código da R3 já no working tree, prompt da R2): 31/31, 99%.

**A única falha era do juiz, não da Luna.** `tamanho-composto-pm-aparece-para-m`
reprovou 2× com o mesmo texto: *"separei essas opções … no tamanho M"*. Os 3 cards eram
`T:M` puro (ids 10002877, 16652825, 8060626); os 2 itens `P/M` estavam no pool e a Luna
**não os recomendou**. O check passava ao juiz só o *conjunto* de tamanhos que a tool
devolveu — ele via `P/M` na lista e reprovava. Violação da POLITICA_DE_GATE §3.11
(medir o que o cliente recebe). Correção: o check agora informa o tamanho de **cada item
recomendado** e a cláusula (3) da rubrica só vale para item recomendado/citado.
Nota para o histórico: antes de abrir o log eu tinha atribuído a falha a "o 3.8
ignorou a instrução dinâmica" — estava errado; a instrução disparou E o modelo escolheu
só itens M. Registro para não repetir a atribuição sem ver os ids.

**Testes:** `test_rodada3.py` 57 asserts; `test_tamanho_unico` U5 robusto (56 sumiu do
catálogo; sonda 56→48). `test_rodada1/2/3` + `f6` verdes contra o prompt do banco.

**Fica para uma Rodada 4 (só depois de medir o 3.8 no campo):** A6 latência
(promo sem round-trip, `IA_TIMEOUT_S` 45→30, `TURNO_ORCAMENTO_S` 150→90, "um momento"
aos 12s) — com mediana de 9–15s no 3.8 o A6 perdeu urgência; Extra 2 (inteiro de
11.047 dígitos → `json.loads` → "probleminha técnico").

## Modelos — trocas executadas em 22/09

**Chat:** `gemini-3-flash-preview` (hardcoded ×4) → `GEMINI_MODEL` env, default
`gemini-3.8-flash` (GA). Aceitação 20/20 graves; latência mediana **9,2s** (preview no
mesmo dia: 13–86s, chamadas travando até 914s). A crônica de fantasia passou no 3.8 (n=1).
Juiz do gate segue em modelo distinto (`EVAL_JUDGE_MODEL`).

**Embedding:** `gemini-embedding-001` → `gemini-embedding-2` (768 dims, coluna e RPC
intocadas). Não é toggle: catálogo inteiro re-embedado. Sequência executada:
backup dos 6.916 vetores (68 MB, fora do git) → cache dos 1.564 textos distintos (todas
as lojas; o emb-2 tem cota de RPM apertada, 1 job por vez com backoff) → escrita via
`regenerar_embeddings.py --from-cache` (1.602 pares, **3min12s, 0 erros**, sem API) →
default em `embedding_text.py` → push → Railway 18:05:47. **Janela de busca degradada:
~5 min, após as 18h.** Vetores verificados (`cos(velho,novo)≈0,04–0,06`).

Por quê: o KNN do 001 não trazia o `BABY DOLL DE LIGANETE` ao pool para "camisola de
liganete" (limite do A3); o embedding-2 o coloca em #2/#3. Material (renda, bojo, fio
duplo) mais preciso. Alerta medido: "enfermeira" puxa itens cirúrgicos (a curadoria barra).

**Recalibração que veio junto (obrigatória em qualquer troca de embedding):** a escala de
similaridade mudou; com `JANELA_SIMILARIDADE=0,05` só 5/11 campeãs elegíveis eram
promovidas (001: 9/11). Deltas campeã→topo medidos:
`[0,02 0,02 0,039 0,039 0,043 0,052 0,052 0,071 0,071 0,085 0,115]` → default **0,08**
(devolve 9/11; `test_ranking` 10/14 pares com destaque). `test_ranking_comercial` com
`VETORES_RANKING_ARQ=tests/vetores_ranking_emb2.json` passou. Rollback disponível:
`restaurar_embeddings.py --backup=migrations/backup/backup_embeddings_001_2026-09-22.json`
+ reverter `2c466dc`/`510e794`.

## Ordem de execução

| Rodada | Ataques | Natureza | Gate |
|---|---|---|---|
| 1 | A1, A4, A5 (prompt) | prompt + dicas dinâmicas | 1 rodada |
| 2 | A2, A3 | código: tool nova + curadoria | 1 rodada |
| 3 | A5 (código), Extra 1 guard, Extra 3 canário | código: detector, handoff silencioso, guard, canário | 1 rodada ✅ |
| 4 (se preciso) | A6, Extra 2 | latência, parsing | após medir o 3.8 no campo |

Rodada 1 é a de maior retorno por hora: quase só prompt, ataca os 56% que somem
e os 16% de fricção. A2 é a de maior retorno absoluto (é o que desmente o
anúncio) mas exige tool nova e testes contra o banco.

**Não fazer:** mexer na assinatura da RPC; afrouxar curadoria sem allowlist;
tirar a pergunta de tamanho (só muda de lugar).

## Pré-requisitos do lado da campanha (não é código)

- Segmentação e criativo: 25% do tráfego foi homem procurando conteúdo sexual.
  Nenhum ajuste no bot compensa público errado.
- Mensagem pré-preenchida de preço só depois de A2 no ar.
- Anúncio em horário comercial: madrugada deu 0 vendas e 7 alertas de lixo.

## Métricas da 2ª campanha (medir com `monitor_trafego.py`)

| métrica | 1ª campanha | meta |
|---|---|---|
| 1ª resposta com produto | 8% | ≥40% |
| continuação após abertura de atacado | 44% | ≥70% |
| "a partir de R$" sem consulta | 11 | 0 |
| handoffs por assédio | 62% | <10% |
| acionamentos 0h–6h | 7 | 0 |
| turnos >20s | 11% | <4% |
| clientes que viram erro | 9 | ≤1 |
| pediram frete | 0 | >0 |
