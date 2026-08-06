# Plano de Execução — Luna passa a ENTENDER mídia recebida (áudio + imagem) na WhatsApp Cloud API

## Resumo executivo

Hoje, quando o cliente manda **áudio** ou **foto**, a Luna fica **muda**: em `_ingest_cloud_message` (bot.py:1737) qualquer `type` diferente de `text`/`interactive`/`button` cai no `else` da **linha 1754** (`raw_text = ""`), a guarda da **linha 1756** (`if not raw_text: return`) loga `"[cloud] mensagem sem texto util"` e retorna. A mídia é descartada antes mesmo do reply-to-card.

A superfície de mudança é **mínima e aditiva**:

1. **1 helper de download** — `_cloud_baixar_midia(media_id)` (2 GETs no Graph, Bearer reaproveitado do `_cloud_send`).
2. **2 helpers Gemini** — `transcrever_audio(bytes, mime)` e `descrever_imagem(bytes, mime, caption)` (chamada multimodal inline, texto puro, sem JSON-mode).
3. **1 branch novo** dentro de `_ingest_cloud_message`, inserido **antes** do `else` da linha 1754, que transforma a mídia em `raw_text` marcado e deixa o fluxo existente seguir.

**A lógica de venda NÃO muda.** Nenhuma tool nova, nenhuma alteração em `process_and_respond` (1306), `consultar_estoque_supabase` (312) ou `transferir_para_atendente` (949). A mídia vira texto e reentra pelo mesmo funil de sempre.

---

## Arquitetura em 1 frase

**mídia recebida → baixa bytes → 1 chamada Gemini vira texto → injeta como `raw_text` marcado → reusa 100% o pipeline de venda existente** — exatamente o mesmo padrão que o **reply-to-card** já usa hoje (bot.py:1760-1767), onde um contexto vira prefixo `[...]` colado ao `raw_text` antes de `_enqueue_user_message`.

```
webhook (1784) ─► _ingest_cloud_message (1737)
                    ├─ dedup _cloud_ja_processado (1740)   ← já cobre mídia
                    ├─ _canonical_user_id (1742)            ← já cobre mídia
                    ├─ tipo=="text"/"interactive"/"button"  (1746-1753)  [existente]
                    ├─ tipo=="audio"  ──► _cloud_baixar_midia ─► transcrever_audio   [NOVO]
                    ├─ tipo=="image"  ──► _cloud_baixar_midia ─► descrever_imagem    [NOVO]
                    ├─ else raw_text="" (1754)              [existente, intacto]
                    ├─ if not raw_text: return (1756)       [existente, intacto]
                    ├─ reply-to-card via context.id (1760)  [existente — mídia herda de graça]
                    └─ _enqueue_user_message (1769)         [existente — entrada única]
                          └─ debounce 10s ─► process_and_respond (1306) ─► LLM + tools
```

---

## Peça 1 — Download de mídia (`_cloud_baixar_midia`)

**Onde:** no bloco de helpers de transporte Cloud, junto de `_cloud_send` (bot.py:1465).

**Vars reutilizadas (já existem):** `GRAPH_VERSION` (bot.py:51, default `v22.0`), `WHATSAPP_TOKEN` (bot.py:53), `requests` (bot.py:7). O padrão de header Bearer é o mesmo do `_cloud_send` (bot.py:1470).

**Importante:** o endpoint de lookup do `media_id` **NÃO é** o `_GRAPH_MSG_URL` (bot.py:56) — esse embute o `phone_number_id` e o path `/messages`. O lookup usa só `GRAPH_VERSION` + `media_id`.

```python
_CLOUD_MEDIA_MAX_BYTES = 16 * 1024 * 1024  # 16MB (teto de audio/video da Cloud API; imagem ~5MB)

def _cloud_baixar_midia(media_id):
    """media_id -> (bytes, mime) ou (None, None) em qualquer falha.
    DOIS GETs, ambos com Authorization: Bearer WHATSAPP_TOKEN:
      1) GET graph.facebook.com/{GRAPH_VERSION}/{media_id} -> JSON {url, mime_type, file_size}
      2) GET nessa url (lookaside.fbsbx.com, EFEMERA ~5min) -> bytes
    """
    if not media_id or not WHATSAPP_TOKEN:
        print("[cloud] baixar_midia: media_id/WHATSAPP_TOKEN ausentes")
        return None, None
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}  # sem Content-Type (é GET)
    try:
        # 1) lookup — montar de GRAPH_VERSION, NAO de _GRAPH_MSG_URL
        meta_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}"
        r1 = requests.get(meta_url, headers=headers, timeout=15)
        r1.raise_for_status()
        meta = r1.json() or {}
        media_url = meta.get("url")
        mime = (meta.get("mime_type") or "").split(";")[0].strip()  # tira "; codecs=opus"
        if not media_url:
            print(f"[cloud] baixar_midia: sem url no lookup | id={media_id}")
            return None, None
        declared = int(meta.get("file_size") or 0)
        if declared and declared > _CLOUD_MEDIA_MAX_BYTES:
            print(f"[cloud] baixar_midia: file_size {declared} > max | id={media_id}")
            return None, None

        # 2) download — MESMO Bearer; a URL lookaside NAO é CDN pública (sem header = 401)
        r2 = requests.get(media_url, headers={**headers, "User-Agent": "curl/8"},
                          timeout=30, stream=True)
        r2.raise_for_status()
        buf = bytearray()
        for chunk in r2.iter_content(64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _CLOUD_MEDIA_MAX_BYTES:
                print(f"[cloud] baixar_midia: excedeu cap | id={media_id}")
                return None, None
        if not buf:
            return None, None
        return bytes(buf), (mime or None)
    except Exception as e:
        print(f"[cloud] baixar_midia falhou | id={media_id}: {e}")
        return None, None
```

**Cuidados:**
- **Bearer nos DOIS GETs.** A URL de download (`lookaside.fbsbx.com`) volta do lookup mas continua exigindo `Authorization: Bearer WHATSAPP_TOKEN`; sem header dá 401.
- **User-Agent no 2º GET** — alguns ambientes rejeitam o download do lookaside sem ele.
- **Timeouts explícitos** (15s lookup, 30s download), igual ao resto do arquivo (`timeout=30`). Nunca `requests` sem timeout.
- **Cap de tamanho** por `file_size` do lookup **e** durante o `stream` (`iter_content`), pois `file_size` pode não vir.
- **URL efêmera (~5min):** baixar **na hora do webhook**, dentro de `_ingest_cloud_message`. Não cachear/reusar.
- **Contrato de erro `(None, None)`** para toda falha — o caller decide o fallback.

**Confirmar:** o mime de áudio do WhatsApp costuma vir como `audio/ogg; codecs=opus`; o `.split(";")[0]` remove os parâmetros que o Gemini pode rejeitar. Confirmar no smoke test qual mime real chega.

---

## Peça 2 — Helpers Gemini (`transcrever_audio`, `descrever_imagem`)

**Onde:** ao lado dos demais helpers (perto de `_cloud_baixar_midia` ou logo abaixo de `get_embedding`, bot.py:196).

**Vars/padrões reutilizados:**
- `genai.configure(api_key=...)` já roda no import (bot.py:65) — os helpers **só instanciam** `genai.GenerativeModel(...)`, sem reconfigurar chave.
- `SAFETY_SETTINGS` (bot.py:20-28) — passar nos **dois** helpers. Domínio lingerie bate em `PROHIBITED_CONTENT` sem `BLOCK_NONE`. Pode ser `None` se o import de `HarmCategory` falhou (bot.py:26-28) — passar `None` mesmo assim (o SDK aceita).
- Modelo: **`gemini-3-flash-preview`**, string idêntica à do pipeline de venda (usada em 1372) — não introduzir outro `model_name`.
- Timeout por chamada: `request_options={"timeout": N}`, mesmo mecanismo do `get_embedding` (bot.py:204) e do `chat.send_message` (bot.py:1382).
- **NÃO** passar `GENERATION_CONFIG` (bot.py:1030) — ele força `response_mime_type=application/json` + `response_schema` do schema de VENDA; a saída sairia como JSON e quebraria a transcrição/descrição. Os helpers instanciam o modelo **sem** `generation_config`, texto puro.

Chamada multimodal inline (não há precedente no arquivo — só chat de texto e `embed_content` — então é padrão novo): `generate_content([prompt, {"mime_type":..., "data": bytes}])`.

```python
MODEL_MULTIMODAL = "gemini-3-flash-preview"  # mesmo modelo do pipeline de venda

def transcrever_audio(audio_bytes, mime):
    """Transcreve áudio recebido do cliente (dados inline, sem File API).
    Retorna a transcrição (str) ou None em falha/vazio."""
    if not audio_bytes:
        return None
    try:
        model = genai.GenerativeModel(MODEL_MULTIMODAL, safety_settings=SAFETY_SETTINGS)
        prompt = ("Transcreva o audio a seguir em portugues do Brasil. "
                  "Responda APENAS com a transcricao literal do que foi dito, "
                  "sem comentarios, sem aspas, sem rotulos.")
        resp = model.generate_content(
            [prompt, {"mime_type": mime or "audio/ogg", "data": audio_bytes}],
            request_options={"timeout": 45},
        )
        texto = (getattr(resp, "text", "") or "").strip()
        return texto or None
    except Exception as e:
        # resp.text tambem LEVANTA se finish_reason=safety/recitation ou sem candidatos
        print(f"[gemini] transcrever_audio falhou: {e}")
        return None


def descrever_imagem(img_bytes, mime, caption=None):
    """Descreve imagem recebida do cliente (peca/foto). Texto puro, sem JSON-mode.
    Retorna a descrição (str) ou None em falha/vazio."""
    if not img_bytes:
        return None
    try:
        model = genai.GenerativeModel(MODEL_MULTIMODAL, safety_settings=SAFETY_SETTINGS)
        prompt = ("Voce ajuda uma loja de lingerie. Descreva de forma objetiva e curta "
                  "a peca na imagem para busca no catalogo: tipo (sutia/calcinha/conjunto/"
                  "camisola etc), cor, estampa, detalhes (renda, boju, fio) e QUALQUER "
                  "codigo/numero/texto visivel na foto ou etiqueta. Se nao for uma peca de "
                  "lingerie, diga o que aparenta ser em 1 frase. Nao invente codigo.")
        if caption:
            prompt += f' Legenda enviada pelo cliente: "{caption}".'
        resp = model.generate_content(
            [prompt, {"mime_type": mime or "image/jpeg", "data": img_bytes}],
            request_options={"timeout": 45},
        )
        texto = (getattr(resp, "text", "") or "").strip()
        return texto or None
    except Exception as e:
        print(f"[gemini] descrever_imagem falhou: {e}")
        return None
```

**Retorno/erro:** contrato uniforme — devolvem `str` não-vazia em sucesso ou **`None`** em qualquer falha (exceção do SDK, `resp.text` que levanta, string vazia). O caller cai no fallback.

**Nota de custo/estado:** cada helper cria um `GenerativeModel` novo (1 chamada por mídia), sem estado, sem `tools`/`system_instruction`/JSON-schema — barato e independente do modelo de venda. Dados inline evitam a File API; mídia de WhatsApp (<16MB) fica dentro do limite de payload inline do Gemini.

---

## Peça 3 — Branch novo em `_ingest_cloud_message`

**Pseudo-diff.** O `else` atual é a **linha 1754** (`raw_text = ""  # midia/localizacao/etc — sem texto util por enquanto`). Inserimos os `elif` de mídia **antes** dele; tudo abaixo (guarda 1756, reply-to-card 1760-1767, enqueue 1769) fica **intacto**.

```python
    elif tipo == "button":
        raw_text = (msg.get("button") or {}).get("text", "")

    # ==================== NOVO: audio recebido ====================
    # NB: Cloud API NUNCA manda type "voice"; nota de voz (PTT) chega como
    #     type "audio" com msg["audio"]["voice"]==true. Casar só tipo=="audio".
    elif tipo == "audio":
        media = msg.get("audio") or {}
        b, mime = _cloud_baixar_midia(media.get("id"))
        txt = transcrever_audio(b, mime) if b else None
        if not txt:
            enviar_mensagem_whatsapp(
                numero, "Nao consegui ouvir seu audio 😅 me escreve o que precisa?")
            return  # fallback proprio; NAO enfileira, NAO cai no 'sem texto util'
        raw_text = f"[transcricao de audio do cliente:] {txt}"

    # ==================== NOVO: imagem recebida ====================
    elif tipo == "image":
        media = msg.get("image") or {}
        caption = (media.get("caption") or "").strip()
        b, mime = _cloud_baixar_midia(media.get("id"))
        desc = descrever_imagem(b, mime, caption) if b else None
        if not desc:
            enviar_mensagem_whatsapp(
                numero, "Recebi sua foto mas nao consegui abrir 😅 "
                        "me manda o codigo da peca ou descreve pra mim?")
            return
        # MESMO molde do reply-to-card: marcador entre colchetes, autossuficiente
        raw_text = f"[o cliente enviou uma foto que parece: {desc}]"
        if caption:
            raw_text += f' Legenda do cliente: "{caption}"'

    else:
        raw_text = ""  # (1754) localizacao/contacts/video/sticker/document — inalterado
    if not raw_text:                                    # (1756) inalterado
        print(f"[cloud] mensagem sem texto util | type={tipo} | from={numero}")
        return
    # reply-to-card (1760-1767) inalterado — mídia com context.id herda o pid de graça
    ...
    _enqueue_user_message(numero, raw_text)             # (1769) inalterado
```

**Tratamento por tipo:**
- **`audio`** → transcrição pura, prefixada com `[transcricao de audio do cliente:]`. PTT e arquivo de áudio caem ambos aqui (não existe type `voice`).
- **`image`** → descrição marcada `[o cliente enviou uma foto que parece: ...]` + legenda do cliente se houver.
- **`video` / `document` / `sticker` / `location` / `contacts`** → **fora do escopo desta fase**; continuam no `else` (1754) → `return`. (Ver Fase 4 opcional.)

**Fallbacks (Peça central do design):** em falha de download **ou** de Gemini (`None`/vazio), o branch chama `enviar_mensagem_whatsapp(numero, ...)` (bot.py:1518, branch cloud → `_cloud_send`) pedindo texto/código e faz `return` **próprio** — não enfileira nada e não cai no log `"sem texto util"`.

**Ordem crítica confirmada no código real:**
- `_cloud_ja_processado(wamid)` (bot.py:1740) e `_canonical_user_id` (bot.py:1742) rodam **antes** do branch por tipo → dedup e normalização já cobrem mídia, e o download só acontece **depois** da checagem de dedup (não gasta Gemini em reentrega da Meta).
- O `elif` de mídia produz `raw_text` **acima** da guarda 1756, então a mídia **não** é descartada e ainda passa pelo reply-to-card 1760.

---

## Por que a venda não muda

`_enqueue_user_message` (bot.py:1716) é a **entrada única e compartilhada** do pipeline (Cloud e UAZAPI). Recebe só `(user_id, raw_text)` — não distingue origem do texto. A transcrição/descrição entra por ela exatamente como uma mensagem digitada.

Em `process_and_respond` (bot.py:1306) o buffer vira `texto_completo` e vai para `chat.send_message(texto_completo, request_options={"timeout": 60})` (bot.py:1382) com `enable_automatic_function_calling=True` (bot.py:1380). **É o LLM que decide, a partir do CONTEÚDO textual**, chamar `consultar_estoque_supabase` (bot.py:312 — busca semântica via `get_embedding` + RPC `buscar_produtos_semantico`), `consultar_produto_por_id` (461), ou `transferir_para_atendente` (949). Nenhuma dessas tools é chamada diretamente pelo código.

Logo: basta a foto/áudio virar texto que o gatilho de busca/handoff é **idêntico** ao de uma mensagem escrita. Zero tool nova, zero mudança em `process_and_respond`.

---

## Fallbacks e mensagens

| Situação | Mensagem sugerida (via `enviar_mensagem_whatsapp`) |
|---|---|
| Áudio: download ou transcrição falhou/vazio | `"Nao consegui ouvir seu audio 😅 me escreve o que precisa?"` |
| Imagem: download ou descrição falhou/vazio | `"Recebi sua foto mas nao consegui abrir 😅 me manda o codigo da peca ou descreve pra mim?"` |

Regra: no fallback, **responder e `return`** — nunca enfileirar.

---

## Handoff (imagem) — gancho, sem bloquear

`transferir_para_atendente` (bot.py:949) já é tool do LLM, criada por `criar_tool_transferir(user_id)` (bot.py:946) e registrada na lista de tools de `process_and_respond` (bot.py:1365). Como a **descrição da imagem entra como texto no histórico**, o próprio LLM pode chamar essa tool passando a descrição dentro de `resumo`/`produtos_interesse` — **sem código novo**.

Se um dia quiser anexar a descrição de forma **determinística** ao handoff, o ponto físico é o `insert` em `conversation_handoffs` dentro de `transferir_para_atendente` (bot.py:990) — coluna nova ou concatenar em `resumo`. **Não implementar agora.**

**Confirmar:** o handoff no modo cloud depende de `operator_number`/`_montar_mensagem_operador` (bot.py:986) estarem configurados no provider cloud — validar antes de prometer handoff de imagem ponta-a-ponta.

---

## Riscos e limites

- **Latência/timeout do webhook (principal risco).** `_ingest_cloud_message` roda **dentro do request do Flask** (bot.py:1784-1802 chama de forma síncrona). Download + chamada Gemini acontecem **antes** do `_enqueue_user_message` — ou seja, **antes** do 200 devolvido à Meta. O debounce de 10s (bot.py:1731) só afeta `process_and_respond`, não o handler. Se download+Gemini demorarem, a Meta pode considerar timeout e **reentregar** (o dedup em 1740 protege contra processar duas vezes, mas não contra o custo). **Mitigação nesta fase:** timeouts curtos (15s/30s no download, 45s no Gemini). **Mitigação recomendada (avaliar):** mover download+transcrição/descrição para uma `threading.Thread` e retornar 200 imediato, injetando via `_enqueue_user_message` de dentro da thread. **Confirmar** o limite de timeout real do webhook da Meta antes de decidir.
- **Tamanho/duração de áudio.** Cap de **16MB** no helper. Áudios muito longos aumentam latência e custo — cap protege memória e tempo. **Confirmar** duração típica dos PTT dos clientes.
- **Custo.** +1 download + **1 chamada Gemini por mídia**. Baixo por mensagem; escala linear com volume de mídia.
- **Política de conteúdo (lingerie/sexshop).** `SAFETY_SETTINGS=BLOCK_NONE` mitiga o filtro por-request, mas o filtro **central** do Google pode bloquear mesmo assim (como já comentado em bot.py:15-17). Nesse caso `resp.text` levanta → helper retorna `None` → fallback pedindo descrição/código. Comportamento degradado, não quebra.
- **Imagem → SKU.** A descrição **não garante** match de produto exato; o LLM faz busca semântica com o texto descrito. **Não prometer** identificação de código pela foto. O prompt já instrui "nao invente codigo".
- **Dedup/reentrega.** Coberto por `_cloud_ja_processado` (bot.py:1665, deque+set, max 3000, best-effort por-processo). Em restart do processo o set zera — reentrega rara pós-deploy pode reprocessar. Aceitável.
- **Concatenação no buffer.** `_enqueue_user_message` concatena mensagens em <10s com espaço. Foto + texto separados vêm colados em `texto_completo` — por isso o marcador `[o cliente enviou uma foto que parece: ...]` é **autossuficiente** (fecha em `]`).
- **Silêncio pós-handoff.** Durante `_em_silencio_pos_handoff` (bot.py:186, checado em 1343) o bot não responde, mas `save_message` (bot.py:1317) grava o texto. Mídia transcrita/descrita durante o silêncio fica visível ao atendente no `chat_history` — desejável. Note: fica salva a **transcrição/descrição**, não a mídia.

---

## Testes

**Unit offline (sem tocar em rede/LLM):**
- Mockar `requests.get` para os 2 GETs de `_cloud_baixar_midia`: (1) JSON `{"url": ..., "mime_type": "audio/ogg; codecs=opus", "file_size": 1234}`, (2) resposta com `.content`/`iter_content` de bytes fake. Assert: retorna `(bytes, "audio/ogg")` (mime sem `; codecs=opus`); 401 no 2º GET → `(None, None)`; `file_size` acima do cap → `(None, None)`; token ausente → `(None, None)`.
- Mockar `genai.GenerativeModel` em `transcrever_audio`/`descrever_imagem`: `resp.text` normal → string; `resp.text` que levanta (simular safety) → `None`; bytes vazios → `None`.
- `_ingest_cloud_message` com `msg` de `type="audio"` e `type="image"` (mockar `_cloud_baixar_midia` e os helpers): assert que `_enqueue_user_message` é chamado com `raw_text` marcado; em falha, assert que `enviar_mensagem_whatsapp` é chamado e `_enqueue_user_message` **não**. Reaproveitar a suíte existente em `tests/eval/` (18 cenários, envios stubbed).
- Caso reply-to-card + imagem: `msg["context"]["id"]` presente → assert que o `pid` (via `_lookup_card`, bot.py:1702) é prefixado à descrição.

**Smoke no número real (+55 27, com `WHATSAPP_PROVIDER=cloud`):**
1. Enviar **1 áudio** ("quero um conjunto vermelho renda tamanho M") → conferir nos logs a transcrição e que a Luna responde com busca de produto.
2. Enviar **1 foto** de peça → conferir a descrição marcada no log e a resposta.
3. Enviar **1 áudio corrompido/curto** ou forçar falha → conferir o texto de fallback.
4. Conferir latência do webhook e ausência de reentrega duplicada nos logs.

---

## Ordem de execução (fases curtas)

- **Fase 0 — confirmar.** Ler mime real de áudio/imagem que chega no webhook; confirmar timeout do webhook da Meta; confirmar handoff cloud configurado.
- **Fase 1 — Peça 1.** `_cloud_baixar_midia` + constante `_CLOUD_MEDIA_MAX_BYTES`, ao lado de `_cloud_send` (1465). Testar isolado com mock.
- **Fase 2 — Peça 2.** `transcrever_audio` + `descrever_imagem` + `MODEL_MULTIMODAL`. Testar com mock de `genai`.
- **Fase 3 — Peça 3.** Branch `audio`/`image` em `_ingest_cloud_message` antes do `else` (1754) + fallbacks. Rodar `tests/eval/` (baseline 100%).
- **Fase 4 — smoke real** no +55 27 (áudio + foto + falha).
- **Fase 5 (opcional/avaliar).** Mover download+Gemini para thread com 200 imediato, se o timeout do webhook se mostrar apertado. Estender para `document`/`sticker` se houver demanda.

---

## O que NÃO muda

- `webhook` (1784) — mídia já chega em `_ingest_cloud_message`.
- `_cloud_ja_processado` (1665), `_canonical_user_id` (1677) — já cobrem mídia (rodam antes do branch).
- Guarda `if not raw_text: return` (1756) e bloco reply-to-card (1760-1767) — permanecem literais; mídia os herda.
- `_enqueue_user_message` (1716), `process_and_respond` (1306), `GENERATION_CONFIG` (1030), lista de `tools` (1361-1366), `consultar_estoque_supabase` (312), `transferir_para_atendente` (949) — **zero alteração**.
- `genai.configure` (65) e `SAFETY_SETTINGS` (20-28) — reutilizados, não redefinidos.