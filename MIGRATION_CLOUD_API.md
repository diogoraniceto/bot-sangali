# Plano de migração — UAZAPI → WhatsApp Business Cloud API (oficial da Meta)

> Bot Sangali (Luna). Objetivo: sair da API não-oficial (UAZAPI/Baileys) — causa provável do banimento de 14/07/2026 — para a **Cloud API oficial da Meta**, que é dentro dos Termos e não bane por uso da ferramenta.
>
> Base do plano: mapeamento do `bot.py` (linhas conferidas contra o arquivo real em 14/07/2026). Os trechos de código nas seções seguintes eram *esboços*; a implementação real (Fase 2) já está no código — ver STATUS abaixo.

---

## STATUS (14/07/2026) — Fase 2 IMPLEMENTADA no código, atrás do flag

A camada de transporte dual-mode já está em `bot.py`, controlada por **`WHATSAPP_PROVIDER`**:
- `uazapi` (**default**) = comportamento atual 100% intacto.
- `cloud` = WhatsApp Business Cloud API oficial da Meta.

**O que já está pronto e testado (offline):**
- Dispatch por provider nas 2 funções de envio (`enviar_mensagem_whatsapp`, `enviar_midia_whatsapp`) — retornam o `wamid` no cloud; timeout de 30s adicionado (fix do hang-risk).
- Webhook: rota `GET /webhook` (handshake `hub.challenge`), validação de assinatura `X-Hub-Signature-256` (HMAC), parse do formato Cloud (`entry[].changes[].value.messages[]`), ignora `value.statuses[]` e eventos não-`messages`, dedup por `wamid`.
- Reply-to-card via `context.id` → lookup em `card_envios` (o card, ao ser enviado no cloud, persiste `wamid → id_produto`).
- `_canonical_user_id` (trata o 9º dígito de celular BR).
- Buffer/debounce/rate-limit unificados num `_enqueue_user_message` compartilhado pelos dois providers.
- Validação: `tests/test_cloud_migration.py` (22 checks, tudo mockado, zero rede) + suíte de conformidade em uazapi-mode sem regressão.

**Falta (lado Meta + cutover — depende da análise do ban):**
1. Provisionar na Meta: WABA + número verificado + display name + System User token + App + App Secret + assinar o campo `messages` no webhook.
2. **Criar a tabela `card_envios`** no Supabase (DDL abaixo).
3. Setar as env vars no Railway (abaixo) e virar `WHATSAPP_PROVIDER=cloud`.
4. Templates para mensagens proativas (handoff/alerta) — Fase 4; hoje, no cloud, esses envios proativos fora da janela de 24h só passam com template aprovado (o alerta de sync já pode ir por e-mail).

**Env vars da Cloud API (Railway, antes do cutover):**
```
WHATSAPP_PROVIDER=cloud
WHATSAPP_PHONE_NUMBER_ID=<do App>
WHATSAPP_TOKEN=<System User token permanente>
WHATSAPP_VERIFY_TOKEN=<string que você inventa; a mesma no painel da Meta>
WHATSAPP_APP_SECRET=<App Secret, para validar a assinatura>
# GRAPH_VERSION=v22.0   (opcional; default já é v22.0)
```

**Coletado (19/07/2026) — número de TESTE da Meta (para smoke test, descartável):**
- `WHATSAPP_PHONE_NUMBER_ID` = `1167504046453758`  (número de teste +1 555 173-0281)
- WhatsApp Business Account ID (WABA) = `4531258517098892`
- App = "Sangali Bot" (portfólio novo "Bot Vendas", business_id 1489598346272137 — o DMRA estava com restrição de anúncios).
- Verify Token escolhido: `sangali_luna_webhook_2026` (mesmo valor no painel da Meta e na env do Railway).
- Token de acesso: **TEMPORÁRIO (~24h)**, do painel "Configuração da API". NÃO é o permanente — usar só no smoke test; gerar **System User token** antes do go-live.
- Ainda falta pegar: **App Secret** (Configurações do app → Básico) e o **System User token permanente**.
- O número real da operação entra na "Etapa 2. Configuração da produção" (não usar o número de teste em produção).

**DDL da tabela de reply-to-card (rodar no Supabase antes do cutover):**
```sql
create table if not exists card_envios (
  wamid       text primary key,
  user_id     text not null,
  id_produto  bigint not null,
  legenda     text,
  created_at  timestamptz default now()
);
create index if not exists idx_card_envios_user on card_envios (user_id, created_at desc);
```
Sem essa tabela, o código degrada em silêncio (loga e segue) — o reply-to-card simplesmente não recupera o id até a tabela existir.

> **Bônus (item b):** o card agora mostra o **preço de atacado** quando o turno é de atacado. O modelo seta `modo_preco` (`varejo` | `atacado_avista` | `atacado_aprazo`) no JSON; em atacado, o card vira `Atacado à vista: R$ X/un (de R$ Y no varejo)`. Requer republicar o `prompt_luna_v2.txt` no banco (regra nova no §0). Independe do provider.

---

## 1. Resumo executivo

**Boa notícia — a superfície é mínima e centralizada:**
- **Todo envio** passa por 2 funções: `enviar_mensagem_whatsapp` ([bot.py:1414](bot.py#L1414)) e `enviar_midia_whatsapp` ([bot.py:1384](bot.py#L1384)). Migrar essas duas cobre 100% dos envios.
- **Todo recebimento** passa por 1 rota: `webhook()` ([bot.py:1541](bot.py#L1541)).
- `sync_erp.py` e `sync_images.py` **não** tocam no WhatsApp — não mudam.
- O painel (`bot-control-panel`) **não** tem acoplamento com provedor — não muda.
- Número de destino já é E.164 sem `+` → compatível com o campo `to` da Cloud API sem mudança.

**As 5 partes trabalhosas (por gravidade):**
1. **Reply-to-card quebra.** Hoje, quando o cliente usa "Responder" num card, a UAZAPI devolve a legenda citada e a gente extrai o `id_produto` do texto `cód: {id}` via regex. A Cloud API **não devolve o texto citado** — só o `context.id` (o wamid da mensagem original). Solução: ao enviar cada card, guardar `wamid → id_produto`; no reply, olhar esse mapa.
2. **Janela de 24h.** Mensagem proativa (fora de uma conversa iniciada pelo cliente nas últimas 24h) **exige template aprovado**. Só 2 envios do bot são proativos: handoff ao operador ([bot.py:965](bot.py#L965)) e alerta do watchdog ([bot.py:1659](bot.py#L1659)). Todo o resto é resposta reativa (dentro da janela) → texto livre continua valendo.
3. **Reescrever o webhook.** Formato totalmente diferente: `entry[].changes[].value.messages[]` (array), `from` no lugar de `chatid`, sem `fromMe`, ignorar `value.statuses[]`, e adicionar **verificação GET (hub.challenge)** + **validação de assinatura X-Hub-Signature-256**.
4. **9º dígito BR.** O `wa_id` da Cloud API pode divergir do `chatid` da UAZAPI no 9º dígito de celular. Como `user_id` é a chave de histórico/handoff/rate-limit, isso pode **fragmentar a identidade** do mesmo cliente. Precisa de uma função única de canonicalização.
5. **Contrato das 2 funções de envio.** Header muda de `{"token": ...}` para `Authorization: Bearer`; corpo ganha `messaging_product` e endpoint único `/messages`; legenda de imagem vira `caption`; e ambas passam a **retornar o wamid** (necessário pro item 1).

**Esforço estimado:** o código é ~2–4 dias de dev. O gargalo real é o **lado da Meta** (verificação de número, aprovação de display name e de templates), que tem tempo de análise (horas a alguns dias).

---

## 2. Duas decisões que você precisa tomar antes

### Decisão A — Cloud API direto vs. BSP
| | **Direto (Meta)** | **Via BSP** (360dialog, Gupshup, Zenvia, Take Blip, Twilio) |
|---|---|---|
| Custo da ferramenta | Grátis (só paga conversa à Meta) | Grátis a ~€/R$ fixo/mês, dependendo do BSP |
| Setup | Você monta App + WABA + webhook + token no Meta Business | BSP simplifica onboarding (embedded signup), hospeda número, dá painel |
| Complexidade técnica | Maior (você cuida de tudo) | Menor (BSP abstrai parte) |
| Recomendado para | Quem quer controle total e menor custo | **Operação enxuta que quer subir rápido e com suporte** |

**Recomendação:** para o Sangali (operação pequena, sem time de infra dedicado), começar por um **BSP** (o 360dialog é popular no Brasil por preço fixo e sem markup por mensagem) reduz muito o atrito. O código fica quase idêntico — o BSP expõe a mesma Cloud API (mudam só a base URL e a forma de pegar o token). Se preferir custo zero de ferramenta e controle total, vá **direto**.

### Decisão B — número recuperado vs. número novo
Isto depende do resultado da análise (appeal) do banimento:
- **Se a Meta restaurar o número:** dá pra migrá-lo pra Cloud API. **Porém:** o número precisa ser *deslogado* do app/da UAZAPI antes de registrar na Cloud API (um número não pode estar nos dois ao mesmo tempo), e a migração de um número ativo **apaga o histórico de conversas do aparelho**.
- **Se a análise falhar (número segue banido):** um número banido **provavelmente não pode** ser onboarded na Cloud API enquanto o ban não for revertido (confirmar com a Meta/BSP). Nesse caso, use um **número novo** — inclusive um número virgem (nunca usado no WhatsApp comum) é o cenário mais limpo pra Cloud API. Contra: os clientes conhecem o número antigo; precisaria comunicar a troca.

> ⚠️ Não reconecte o número na UAZAPI enquanto a análise corre — reconexão automática é o que pode reforçar o ban.

---

## 3. Pré-requisitos na Meta (o que provisionar)

1. **Meta Business Manager** com a empresa **verificada** (Business Verification).
2. **WhatsApp Business Account (WABA)** dentro do Business Manager.
3. **Número de telefone** adicionado à WABA e **verificado** (SMS/voz); definir e aprovar o **display name**.
4. **App** no Meta for Developers com o produto *WhatsApp* → pega-se o **Phone Number ID** e o **WhatsApp Business Account ID**.
5. **Token de acesso permanente** = criar um **System User** (Business Settings) com permissão na WABA e gerar token que não expira. (O token temporário de 24h só serve pra teste.)
6. **App Secret** (do App) — usado pra validar a assinatura do webhook.
7. **Webhook** configurado no App apontando pra `https://bot-sangali-production.up.railway.app/webhook`, com um **Verify Token** (string que você inventa) e assinando o campo `messages`.
8. **Templates** aprovados (ver §6) — só se for manter handoff/alertas no WhatsApp.
9. Durante o modo de teste (antes da verificação completa), o número só envia pra uma **allow-list** — adicione o `operator_number` e o `ALERT_WHATSAPP` como destinatários de teste, senão eles recebem nada silenciosamente.

---

## 4. Mudanças no código (`bot.py`)

### 4.1 Configuração (substitui [bot.py:40-41](bot.py#L40))
```python
# REMOVER: UAZAPI_URL, UAZAPI_TOKEN
GRAPH_VERSION            = os.getenv("GRAPH_VERSION", "v22.0")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TOKEN           = os.getenv("WHATSAPP_TOKEN")          # System User token (permanente)
WHATSAPP_VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN")   # string que você inventa
WHATSAPP_APP_SECRET      = os.getenv("WHATSAPP_APP_SECRET")     # do App, p/ validar assinatura
GRAPH_MSG_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
# (Com BSP, muda só a base URL e a origem do token.)
```

### 4.2 As 2 primitivas de envio — agora retornam o `wamid`
Esboço (substitui [bot.py:1384-1421](bot.py#L1384)):
```python
_WA_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def enviar_mensagem_whatsapp(numero, texto):
    payload = {"messaging_product": "whatsapp", "to": numero,
               "type": "text", "text": {"body": texto, "preview_url": False}}
    try:
        r = requests.post(GRAPH_MSG_URL, headers=_WA_HEADERS, json=payload, timeout=30)  # timeout novo!
        r.raise_for_status()
        return (r.json().get("messages") or [{}])[0].get("id")   # wamid p/ o reply-to-card
    except Exception as e:
        print(f"[wa] erro envio texto: {e}")
        return None

def enviar_midia_whatsapp(numero, url_midia, legenda):
    payload = {"messaging_product": "whatsapp", "to": numero,
               "type": "image", "image": {"link": url_midia, "caption": legenda}}
    try:
        r = requests.post(GRAPH_MSG_URL, headers=_WA_HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        return (r.json().get("messages") or [{}])[0].get("id")
    except Exception as e:
        print(f"[wa] erro envio midia: {e}")
        return None
```
- `caption` no lugar de `text`; `image.link` no lugar de `file`.
- **Timeout** resolve o risco atual de travar thread (hoje os `requests.post` de envio não têm timeout — [bot.py:1409](bot.py#L1409), [bot.py:1419](bot.py#L1419)).
- **Mídia por link:** a Meta busca a URL no servidor dela. As fotos vêm do host de imagens do ERP GestaoClick (não do Supabase). Se a Meta rejeitar o fetch (host/MIME/cache/>5MB), o plano B é baixar os bytes e subir em `/{PHONE_NUMBER_ID}/media` → enviar por `image.id`, ou re-hospedar no Supabase Storage. **Testar cedo.**

### 4.3 Capturar o wamid do card (para o reply)
Em `renderizar_mensagem_estruturada` ([bot.py:1175-1204](bot.py#L1175)), o envio do card em [bot.py:1197](bot.py#L1197) passa a guardar o retorno:
```python
wamid = enviar_midia_whatsapp(user_id, url, legenda)   # ou enviar_mensagem_whatsapp p/ card sem foto
if wamid:
    registrar_card_enviado(wamid, user_id, id_produto, legenda)
```

Nova tabela no Supabase (mapa wamid → produto):
```sql
create table if not exists card_envios (
  wamid       text primary key,
  user_id     text not null,
  id_produto  bigint not null,
  legenda     text,
  created_at  timestamptz default now()
);
create index on card_envios (user_id, created_at desc);
```

### 4.4 Webhook novo (substitui [bot.py:1541-1601](bot.py#L1541))
```python
import hmac, hashlib

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data()                      # bytes crus p/ o HMAC (antes de parsear!)
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return "bad signature", 403

    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":     # ignora template/quality/account updates
                continue
            value = change.get("value", {})
            for msg in value.get("messages", []):      # ignora value["statuses"] (recibos)
                _processar_msg_cloud(msg, value)
    return "ok", 200                                    # responder 200 rápido (evita reentrega)

def _processar_msg_cloud(msg, value):
    wamid = msg.get("id")
    if _ja_processado(wamid):        # dedup: Meta reenvia se não receber 200 rápido
        return
    numero = canonical_user_id(msg.get("from"))         # canonicaliza 9º dígito BR
    tipo   = msg.get("type")
    if tipo == "text":
        raw_text = msg.get("text", {}).get("body", "")
    elif tipo == "interactive":                          # botões/listas, se usar
        inter = msg.get("interactive", {})
        raw_text = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
    else:
        return                                           # mídia recebida: tratar se/quando precisar (§4.6)

    # reply-to-card: contexto traz só o wamid citado -> lookup
    ctx_id = (msg.get("context") or {}).get("id")
    if ctx_id:
        pid = lookup_card(ctx_id)                        # SELECT id_produto FROM card_envios WHERE wamid=ctx_id
        if pid:
            raw_text = (f"[o cliente respondeu ao card do produto id_produto={pid}. "
                        f"Use EXATAMENTE id_produto={pid} para este item.] " + raw_text)

    # daqui pra baixo, REAPROVEITA o buffer/debounce/rate-limit existente (agnósticos de provedor)
    ...
```
- **Dedup por wamid** é recomendado (Meta reentrega). Como o Railway pode reiniciar/escalar, o ideal é um store compartilhado (tabela/Redis) — hoje o estado é dict em processo ([bot.py:49-64](bot.py#L49)).
- O buffer/debounce (`message_buffers` + `threading.Timer`, [bot.py:1586-1599](bot.py#L1586)) **não muda** — só muda o que o alimenta (`from` e `text.body`).

### 4.5 Canonicalização do número (9º dígito BR)
```python
def canonical_user_id(num):
    # normaliza para um formato único (ex.: garantir/estabilizar o 9º dígito de celular BR)
    # aplicar em TODA leitura de identidade e nas chaves de chat_history/handoff/rate-limit
    ...
```
Aplicar em `save_message`/`get_history`/handoff/rate-limit para o mesmo cliente não virar duas identidades. Atenção: o histórico já gravado está no formato antigo da UAZAPI.

### 4.6 (Opcional) mídia recebida
Hoje o bot **não** processa mídia recebida (foto/áudio) — se vier sem texto, ele só bufferiza. Se um dia quiser receber (ex.: áudio → transcrição, comprovante), na Cloud API é fluxo de 2 GETs autenticados: `messages[0][<tipo>].id` → `GET /{media_id}` → `GET {url}` com Bearer. Não é bloqueante pra migração.

---

## 5. Mensagens proativas (janela de 24h) — decisão de canal

| Envio | Onde | Hoje | Na Cloud API |
|---|---|---|---|
| Alerta de sync parado | [bot.py:1659](bot.py#L1659) | texto livre p/ admin | **Migrar pra e-mail** — `_enviar_email_alerta` já existe ([bot.py:1638](bot.py#L1638)); só faltam as vars SMTP. Evita template. |
| Handoff → operador | [bot.py:965](bot.py#L965) | texto livre p/ atendente | **Template** `handoff_atendente` (categoria *utility*) **ou** manter a atendente "quente" (respondendo periódico) **ou** notificar por e-mail/Slack. |

**Recomendação:** alerta de sync → **e-mail** (mais simples e robusto). Handoff → **template utility** (mantém no WhatsApp, que é onde a atendente atua).

### 5.1 CONFIRMADO EM PRODUÇÃO (12/08/2026) — e o que já está no código

A previsão acima se materializou, e do jeito mais difícil de perceber. Cronologia medida:

| Quando | Número do operador | Ele havia escrito ao bot? | Resultado |
|---|---|---|---|
| 06/08 22:31 | `...14997767200` | sim, 2,6 h antes | **chegou** |
| 07/08 12:48 | `...14997767200` | sim, 16,9 h antes | **chegou** |
| 12/08 19:00 | `...27997288088` | **nunca** | **não chegou** |

O número antigo era o do próprio desenvolvedor, com 57 mensagens ao bot — estava "quente" **por acidente**, e isso mascarou o defeito desde que o provider virou `cloud` (06/08 18:56, datado pelo primeiro `wamid.*` em `card_envios`). A atendente real nunca escreve ao bot, então está permanentemente fora da janela.

**O que enganou o diagnóstico:** a Meta respondeu **HTTP 200** e devolveu `wamid`. Aceitação não é entrega — a falha vem depois, no webhook de `statuses`, que o código **descartava sem logar** (`# ignora value["statuses"]`). Resultado: "não entregue" era indistinguível de "entregue".

**Já implementado (não depende da Meta):**
- `_cloud_send` loga o **corpo** do erro (`code`, `subcode`, `details`). Antes, `raise_for_status()` produzia "400 Client Error" e jogava o motivo no lixo.
- Webhook lê e loga `statuses`: `failed` com código, e `delivered`/`read`.
- `_cloud_send_template` + `enviar_alerta_operador`: template primeiro, texto livre como degradação.
- `_janela_24h_aberta(numero)`: consulta `chat_history` para responder **agora** se o texto livre vai chegar, em vez de esperar o recibo. Fora da janela, "aceito" é reportado como **não avisado**.
- Handoff **não mente mais**: sem aviso entregue devolve `aviso_nao_entregue`, dispara e-mail, e **não grava** `conversation_handoffs` — porque é essa linha que ativa o silêncio de 2h. Antes, o cliente ouvia "já chamei uma atendente", ninguém era chamado, e a Luna ficava muda por 2 h. Era o pior defeito comercial em aberto.

### 5.2 IDs da conta (não são segredos — são identificadores de objeto)

Guardados aqui porque descobri-los custou uma investigação inteira e o token do bot **não** consegue redescobri-los:

| O quê | Valor |
|---|---|
| App | `1579315293582101` ("Sangali Bot") |
| Portfólio empresarial | `1489598346272137` ("Cyber Suite") |
| Phone Number ID | `1188430681025942` (+55 27 98816-2802, "Sangali", GREEN, LIVE) |
| **WABA ID** | **`1523625905911728`** |
| System User do token | `122104146435395122` ("sangali") |
| Template do handoff | `1793024548719200` (`handoff_atendente`, UTILITY, pt_BR) |

**Como o WABA foi obtido:** pelo `entry.id` do webhook — logado de propósito em `bot.py`. Todos os caminhos de API falham porque o token tem `whatsapp_business_management` mas **não** `business_management`. Se precisar de novo, é o log `[cloud] WABA_ID (entry.id) = ...` na primeira mensagem recebida.

**Vale setar `WHATSAPP_WABA_ID=1523625905911728` no Railway** para que `criar_template_handoff.py --check` rode sem investigação.

**Status:** criado em 12/08 como `PENDING`, **`APPROVED` em 16/08**. Script pronto para recriar/auditar:

```bash
export WHATSAPP_WABA_ID=...   # WhatsApp Business Account ID, NÃO o phone_number_id
export WHATSAPP_TOKEN=...     # precisa de whatsapp_business_management
python criar_template_handoff.py --check    # audita o que existe
python criar_template_handoff.py            # cria o handoff_atendente (utility, pt_BR)
```

Enquanto o status não for `APPROVED`, o código degrada sozinho para texto livre e **avisa no log** que o template resolveria. Nada quebra; só continua não entregando fora da janela.

**O que ainda NÃO foi confirmado:** que a mensagem do template *chegou* ao aparelho do atendente. A Meta aceitou (HTTP 200 + `wamid`), mas nenhum recibo `delivered` foi observado — e aceitação não é entrega (§5.1). O webhook hoje loga `failed`/`delivered`/`read`, mas **não** loga `sent`, então falta um degrau da trilha. Confirmar com o atendente, ou fechar a instrumentação, antes de tratar o handoff como resolvido.

---

## 6. Templates a criar/aprovar (se manter WhatsApp no proativo)
- `handoff_atendente` (utility), ex.: *"Novo atendimento: {{1}} pediu ajuda. Motivo: {{2}}. Resumo: {{3}}"*.
- `sync_alerta` (utility) — só se **não** migrar o alerta pra e-mail.

Aprovação leva de minutos a dias. Se a Meta classificar como *marketing* (não *utility*), há limites de frequência/opt-in mais rígidos.

---

## 7. Ordem de execução sugerida

- **Fase 0 — Decisões (§2):** BSP vs direto; número recuperado vs novo (depende do appeal).
- **Fase 1 — Provisionar Meta (§3):** WABA + número verificado + display name + System User token + App + webhook + App Secret.
- **Fase 2 — Camada nova atrás de flag:** implementar envio+webhook novos com `WHATSAPP_PROVIDER=cloud|uazapi` (dual-mode), pra testar sem arrancar a UAZAPI. Validar assinatura, hub.challenge, envio de texto e de imagem por link.
- **Fase 3 — Reply-to-card + identidade:** tabela `card_envios`, captura de wamid, lookup por `context.id`, e `canonical_user_id`.
- **Fase 4 — Proativo:** mover alerta pra e-mail (setar SMTP) e criar/aprovar template de handoff.
- **Fase 5 — Cutover:** deslogar o número da UAZAPI → registrar na Cloud API → apontar webhook → virar o flag → smoke test com a allow-list.
- **Fase 6 — Religar o Railway** (`deploymentRedeploy`) já no modo Cloud API.

---

## 8. Custos (confirmar preços atuais no site da Meta — mudam com frequência)
- A **ferramenta** (Cloud API) é grátis. Você paga por **conversa/mensagem** conforme a categoria.
- **Conversas iniciadas pelo cliente (service)** — que é ~todo o fluxo de vendas deste bot, reativo — são **gratuitas** (e há tier grátis mensal).
- **Mensagens proativas (template)** têm custo por mensagem por categoria (utility/marketing/authentication). Aqui só handoff/alertas — volume baixíssimo.
- **BSP:** somar a mensalidade/plano do provedor, se escolher um.
- **Conclusão:** para este bot (majoritariamente reativo), o custo de mensageria tende a ser **muito baixo**.

---

## 9. O que NÃO muda
- `sync_erp.py`, `sync_images.py` (só falam com ERP + Supabase).
- Painel `bot-control-panel` (sem acoplamento a provedor; já salva número sem `+`).
- Toda a lógica de IA (Gemini, prompt no banco, busca semântica, cards, `calcular_total`), buffer/debounce/rate-limit, `chat_history`/`bot_turns`/`conversation_handoffs`.
- Não há código de "digitando"/presença nem de read-receipt (não existem hoje; são opcionais de adicionar depois).

---

### Riscos residuais a vigiar
1. Fetch de mídia por link rejeitado pela Meta (host do ERP) → plano B upload/`media_id`.
2. Fragmentação de identidade pelo 9º dígito BR → `canonical_user_id` obrigatória.
3. Reentrega de webhook sem dedup compartilhado → duplo disparo de resposta/handoff.
4. Número banido pode não ser onboarded na Cloud API antes de reverter o ban.
