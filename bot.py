import os
import re
import json
import hmac
import hashlib
import collections
import requests
import time
import threading
import traceback
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import google.generativeai as genai

# Safety relaxado: dominio lingerie/sexshop gera falso-positivo no filtro padrao.
# (block_reason=PROHIBITED_CONTENT e filtro central NAO configuravel aqui -> tratado
# por degradacao graciosa no except de process_and_respond.)
try:
    from google.generativeai.types import HarmCategory as _HC, HarmBlockThreshold as _HB
    SAFETY_SETTINGS = {
        _HC.HARM_CATEGORY_HARASSMENT: _HB.BLOCK_NONE,
        _HC.HARM_CATEGORY_HATE_SPEECH: _HB.BLOCK_NONE,
        _HC.HARM_CATEGORY_SEXUALLY_EXPLICIT: _HB.BLOCK_NONE,
        _HC.HARM_CATEGORY_DANGEROUS_CONTENT: _HB.BLOCK_NONE,
    }
except Exception as _e_safety:
    print(f"[safety] settings nao configurados: {_e_safety}")
    SAFETY_SETTINGS = None
from google.api_core.exceptions import GoogleAPICallError
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from sync_erp import sync_otimizado
import sync_erp
from sync_images import sync_images

# ================= CONFIGURAÇÕES =================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
UAZAPI_URL = os.getenv("UAZAPI_URL")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN")

# ---- Camada de transporte do WhatsApp (migracao UAZAPI -> Cloud API oficial) ----
# WHATSAPP_PROVIDER controla qual backend envia/recebe. Default "uazapi" preserva
# 100% do comportamento atual; "cloud" ativa a WhatsApp Business Cloud API da Meta.
# Ver MIGRATION_CLOUD_API.md.
WHATSAPP_PROVIDER = (os.getenv("WHATSAPP_PROVIDER", "uazapi") or "uazapi").lower()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")            # System User token (permanente)
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")  # string p/ handshake do webhook
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")  # do App, p/ validar X-Hub-Signature-256
_GRAPH_MSG_URL = (
    f"https://graph.facebook.com/{GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    if WHATSAPP_PHONE_NUMBER_ID else None
)

MELHOR_ENVIO_TOKEN = os.getenv("MELHOR_ENVIO_TOKEN")
MELHOR_ENVIO_URL = "https://www.melhorenvio.com.br/api/v2/me/shipment/calculate"

app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

message_buffers = {}
_message_buffers_global_lock = threading.Lock()  # protege escrita no dict global
# user_ids ja identificados em contexto de ATACADO — usado p/ exibir o preco de
# atacado no card (modo_preco) mesmo quando o modelo esquece de setar o campo.
_atacado_users = set()
_user_locks = {}  # threading.Lock() por user_id (criado on-demand)
_user_turn_locks = {}  # threading.Lock() por user_id: serializa o TURNO INTEIRO
                       # (segurado por dezenas de segundos — nunca na thread do webhook)

# A última mensagem do cliente e o user_id do turno vivem em `_turn_ctx`
# (threading.local, abaixo). Eram globais de módulo e vazavam o contexto de um
# cliente para o turno de outro quando dois clientes falavam ao mesmo tempo.

# Rate limit: histórico de timestamps por user_id. {user_id: [ts1, ts2, ...]}
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_MSGS = 10
_rate_limit_history = {}


def _get_user_lock(user_id):
    """Lock CURTO do buffer (só mutação de `message_buffers`) para este user_id.

    Adquirido pela thread do webhook Flask — PROIBIDO segurar por I/O.
    Para serializar o turno inteiro use `_get_turn_lock`.
    """
    with _message_buffers_global_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def _get_turn_lock(user_id):
    """Lock do TURNO deste user_id (cria se não existir).

    NUNCA adquirir na thread do webhook Flask: é segurado por dezenas de segundos
    (Gemini + tools) e estouraria o timeout da Meta, causando reentrega.
    ORDEM DE AQUISIÇÃO: turn_lock -> buffer_lock; nunca o inverso.
    """
    with _message_buffers_global_lock:
        lock = _user_turn_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_turn_locks[user_id] = lock
        return lock


# Blocos que o BOT injeta no texto do turno e que NÃO são fala do cliente:
#   [o cliente respondeu ao card do produto id_produto=6743024 (citacao: "*CAMISOLA* - R$ 49,90 _cod: 6743024_")]
#   [transcrição de áudio do cliente:] / [o cliente enviou uma foto que parece: ...]
# A legenda citada carrega PREÇO, e a parte inteira do preço (30-69) casa com o
# regex de tamanho numérico de `_tamanhos_validos_na_msg` — que devolve os números
# ANTES das letras. Resultado medido: 'R$ 49,90 ... tem no M?' -> ['49','M'], e o
# guard forçava a busca em '49'. 46,5% das linhas com estoque têm preço nessa faixa
# e existem tamanhos numéricos REAIS (40..54), então às vezes o preço "casava" e
# devolvia grade errada com cara de acerto. Daí sanear ANTES de extrair tamanho.
_RE_BLOCO_INJETADO = re.compile(r"\[[^\[\]]*\]")
_RE_PRECO = re.compile(r"R\$\s*[\d.,]+", re.IGNORECASE)
_RE_DECIMAL = re.compile(r"\b\d{1,6}[.,]\d+\b")
_RE_COD = re.compile(r"\b(?:c[oó]d(?:igo)?|id_produto)\s*[:=]?\s*\d+", re.IGNORECASE)


def _texto_cliente_puro(texto):
    """Só o que o CLIENTE escreveu/falou, sem o que o bot injetou no texto do turno.

    Remove (nesta ordem) blocos `[...]` de preâmbulo/citação, preços (`R$ 49,90` e
    decimais soltos) e códigos de produto (`cod: 6743024`, `id_produto=6743024`).
    É a ÚNICA fonte válida para inferir o tamanho que o cliente pediu: usar o texto
    cru faria o guard forçar a parte inteira do preço da legenda como tamanho.
    Preserva a transcrição de áudio e a legenda escrita pelo cliente na foto —
    essas SÃO fala do cliente e ficam fora dos colchetes.
    """
    if not texto:
        return ""
    t = texto
    # loop: bloco removido pode revelar outro (colchetes aninhados no preâmbulo)
    for _ in range(4):
        novo = _RE_BLOCO_INJETADO.sub(" ", t)
        if novo == t:
            break
        t = novo
    t = _RE_PRECO.sub(" ", t)
    t = _RE_COD.sub(" ", t)
    t = _RE_DECIMAL.sub(" ", t)
    return t.strip()


# Contexto do turno NA THREAD atual (cada turno roda na sua Timer thread).
# Substitui os globais de módulo, que vazavam o contexto de um cliente para outro.
_turn_ctx = threading.local()


def _set_turn_ctx(user_id, texto):
    _turn_ctx.user_id = user_id
    _turn_ctx.last_msg = texto
    _turn_ctx.msg_cliente = _texto_cliente_puro(texto)
    _turn_ctx.excluir_ids = []


def _clear_turn_ctx():
    _turn_ctx.user_id = None
    _turn_ctx.last_msg = None
    _turn_ctx.msg_cliente = None
    _turn_ctx.excluir_ids = []


def _ctx_last_msg():
    """Última mensagem do cliente NESTE turno, CRUA (com preâmbulo). None fora de um turno."""
    return getattr(_turn_ctx, "last_msg", None)


def _ctx_msg_cliente():
    """Só a fala do cliente neste turno (sem preâmbulo/legenda/preço). "" fora de um turno.

    Fallback saneando `last_msg` para o caso de contexto setado por caminho antigo.
    """
    v = getattr(_turn_ctx, "msg_cliente", None)
    if v is None:
        return _texto_cliente_puro(_ctx_last_msg())
    return v


def _ctx_user_id():
    """user_id do turno corrente nesta thread. None fora de um turno."""
    return getattr(_turn_ctx, "user_id", None)


def _check_rate_limit(user_id):
    """Retorna True se OK, False se cliente excedeu RATE_LIMIT_MAX_MSGS na janela."""
    now = time.time()
    with _message_buffers_global_lock:
        hist = _rate_limit_history.get(user_id, [])
        # remove timestamps fora da janela
        hist = [t for t in hist if now - t < RATE_LIMIT_WINDOW_SEC]
        hist.append(now)
        _rate_limit_history[user_id] = hist
        return len(hist) <= RATE_LIMIT_MAX_MSGS


def _tamanhos_validos_na_msg(texto):
    """Extrai tokens de tamanho que aparecem em uma mensagem do usuário.

    Retorna lista de tokens canônicos. Se cliente diz só 'G', volta ['G'].
    Se diz 'tamanho 42', volta ['42']. Vazio se nada parecido.
    """
    if not texto:
        return []
    upper = texto.upper()
    # Colapso de repetições óbvias: GGGG -> GG, PPPP -> PP, GGG -> GG (digitação)
    upper = re.sub(r"([PMG])\1{2,}", lambda m: m.group(1) * 2, upper)

    encontrados = []
    # Números 30-69 — tamanho numérico
    for m in re.finditer(r"\b(3[0-9]|4[0-9]|5[0-9]|6[0-9])\b", upper):
        encontrados.append(m.group(1))
    # Letras de tamanho — match de palavra inteira (boundary manual)
    # Ordem de mais longo para mais curto evita pegar "G" dentro de "GG".
    cobertos = set()  # posições já cobertas por token mais longo
    for token in ("XGG", "XGG", "XG", "GG", "PP", "G3", "G2", "G1", "G", "M", "P"):
        for m in re.finditer(r"(?:^|[^A-Z0-9])(" + re.escape(token) + r")(?:$|[^A-Z0-9])", upper):
            if any(p in cobertos for p in range(m.start(1), m.end(1))):
                continue
            for p in range(m.start(1), m.end(1)):
                cobertos.add(p)
            encontrados.append(token)
    # ÚNICO / ÚNICA
    if "ÚNIC" in upper or "UNIC" in upper:
        encontrados.append("UNICO")
    return encontrados


# Espelho EXATO de public.normalize_tamanho_tokens(text) — as duas strings abaixo
# sao copia literal do prosrc da funcao SQL (migration 0001). A RPC casa tamanho por
# `tamanho_tokens && tokens_alvo` (overlap); o Python precisa da MESMA normalizacao,
# senao o re-filtro local derruba o que o banco aprovou.
# Ordem igual ao SQL: translate ANTES de upper. Sem unicodedata/NFKD de proposito.
_TRANS_TAMANHO = str.maketrans(
    "ÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑáàâãäåéèêëíìîïóòôõöúùûüçñ",
    "AAAAAAEEEEIIIIOOOOOUUUUCNAAAAAAEEEEIIIIOOOOOUUUUCN",
)
_SEP_TAMANHO = re.compile(r"[\s\-/(),|]+")


def _tokens_tamanho(txt):
    """Tokens canonicos de um valor de tamanho. Espelha normalize_tamanho_tokens (SQL).

    'G/GG' -> ['G','GG'] · 'ÚNICO' -> ['UNICO'] · '38-40' -> ['38','40'] · '' -> []
    """
    if not txt:
        return []
    norm = str(txt).translate(_TRANS_TAMANHO).upper()
    return [t for t in (tok.strip() for tok in _SEP_TAMANHO.split(norm)) if t]


def _tokens_tamanho_do_cliente():
    """Tokens de tamanho que o CLIENTE escreveu neste turno. Fonte única do guard e do log.

    Lê `_ctx_msg_cliente()` (sem preâmbulo/legenda/preço) — nunca o texto cru.
    """
    return _tamanhos_validos_na_msg(_ctx_msg_cliente())


def _resolver_tamanho_alvo(tamanho_alvo):
    """(tamanho_efetivo, corrigido: bool). Lê o contexto do TURNO, não global de módulo.

    Defesa contra LLM drift (Risco B4): se o cliente disse uma letra ('G') e o
    modelo passou outra ('GG'), vale o que o cliente escreveu nesta mensagem.
    Fora de um turno (`_ctx_msg_cliente()` vazio) o guard não inventa correção.
    """
    if not tamanho_alvo:
        return None, False
    tokens_user = _tokens_tamanho_do_cliente()
    if tokens_user and tamanho_alvo not in tokens_user:
        print(f"[GUARD] tamanho '{tamanho_alvo}' fora da msg do cliente {tokens_user} -> forcando '{tokens_user[0]}'")
        return tokens_user[0], True
    return tamanho_alvo, False


HANDOFF_SILENCIO_MIN = 120  # silencia o bot por 2h apos handoff
CONVERSA_GAP_HORAS = 24     # gap entre msgs que reseta o historico do contexto


def _parse_iso_ts(ts_str):
    """Converte ISO timestamp para datetime UTC. Retorna None se invalido."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _ultimo_handoff_em(user_id):
    """Retorna timedelta desde o último handoff do user, ou None se nunca houve."""
    try:
        r = (
            supabase.table("conversation_handoffs")
            .select("created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[handoff] falha ao consultar conversation_handoffs: {e}")
        return None
    if not r.data:
        return None
    try:
        ts = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
    except Exception as e:
        print(f"[handoff] created_at inválido: {e}")
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts


def _em_silencio_pos_handoff(user_id):
    """True se houve handoff para este user nos últimos HANDOFF_SILENCIO_MIN minutos."""
    delta = _ultimo_handoff_em(user_id)
    if delta is None:
        return False
    return delta < timedelta(minutes=HANDOFF_SILENCIO_MIN)


# ================= AUXILIARES DE INTELIGÊNCIA =================

def get_embedding(text):
    """Gera o vetor semântico para a busca no banco."""
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=text,
            task_type="retrieval_query",
            output_dimensionality=768,
            request_options={"timeout": 30}
        )
        return result['embedding']
    except Exception as e:
        print(f"❌ Erro Embedding: {e}")
        return None

# ================= PERSISTÊNCIA (SUPABASE) =================

def save_message(user_id, role, content):
    """Grava a linha em chat_history e devolve o `id` (uuid) da linha, ou None."""
    r = supabase.table("chat_history").insert({
        "user_id": user_id,
        "role": role,
        "content": content
    }).execute()
    try:
        return (r.data or [{}])[0].get("id")
    except Exception:
        return None

def get_history(user_id, limit=30, excluir_id=None):
    """Carrega historico recente do user, descartando 'conversa antiga'.

    Conversa antiga = mensagens antes de um gap >= CONVERSA_GAP_HORAS, OU
    mensagens antes do ultimo handoff (atendente humana ja resolveu).
    O banco mantem tudo para auditoria — quem filtra eh so o contexto do Gemini.

    excluir_id: id de linha a remover do contexto — usado para tirar a mensagem
    ATUAL do cliente, que ja vai ao modelo via chat.send_message(). Busca
    `limit + 1` nesse caso, entao o contexto efetivo continua sendo `limit`.
    Exclusao por ID, nao por texto: imune a cliente que repete a mesma frase.
    """
    response = supabase.table("chat_history") \
        .select("id, role, content, created_at") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(limit + 1 if excluir_id is not None else limit) \
        .execute()

    rows = response.data or []
    if not rows:
        return []

    # 1. Gap entre msgs consecutivas. rows esta em ordem DESC, entao descemos
    # no tempo procurando o primeiro gap grande — tudo a partir dali pra tras
    # eh outra conversa.
    gap_limit = timedelta(hours=CONVERSA_GAP_HORAS)
    cutoff_idx = len(rows)
    for i in range(len(rows) - 1):
        ts_atual = _parse_iso_ts(rows[i].get("created_at"))
        ts_anterior = _parse_iso_ts(rows[i + 1].get("created_at"))
        if ts_atual and ts_anterior and (ts_atual - ts_anterior) >= gap_limit:
            cutoff_idx = i + 1
            break
    rows = rows[:cutoff_idx]

    # 2. Descarta mensagens anteriores ao ultimo handoff (se houver).
    delta_handoff = _ultimo_handoff_em(user_id)
    if delta_handoff is not None:
        ts_handoff = datetime.now(timezone.utc) - delta_handoff
        rows = [
            r for r in rows
            if (ts := _parse_iso_ts(r.get("created_at"))) and ts > ts_handoff
        ]

    # 2.1. Remove a mensagem atual (ja entregue ao modelo por send_message).
    if excluir_id is not None:
        rows = [r for r in rows if r.get("id") != excluir_id]

    # 3. Reverte para ordem cronologica (oldest -> newest) para o Gemini.
    rows.reverse()
    return [{"role": r["role"], "parts": [r["content"]]} for r in rows]


def _normalizar_history_para_gemini(history):
    """Deixa o history nas duas pontas que a API exige. Muta e devolve a lista.

    CAUDA: colapsa corrida final de turnos 'user' adjacentes (turno que errou deixa
    linha `user` orfa; o cliente reenvia e nascem dois `user` seguidos). Mantem o
    mais recente. Dois turnos `user` adjacentes sao a forma exata que produzia
    respostas do tipo "nao temos GG" para quem tinha pedido G.
    CABECA: a API exige que o historico comece em turno do usuario.
    """
    _n = 0
    while len(history) >= 2 and history[-1]["role"] == "user" and history[-2]["role"] == "user":
        history.pop(-2)
        _n += 1
    if _n:
        print(f"[HIST] cauda user colapsada: {_n}")
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def _job_purge_bot_turns():
    """Roda 1x/dia: chama purge_old_bot_turns() do Postgres para apagar
    registros com mais de 30 dias. Substitui necessidade de pg_cron."""
    try:
        r = supabase.rpc('purge_old_bot_turns', {}).execute()
        rows = r.data if isinstance(r.data, int) else (r.data or 0)
        print(f"[PURGE] bot_turns purgado: {rows} rows removidas")
    except Exception as e:
        print(f"[PURGE] falhou: {e}")


def log_turn(user_id, user_input, tool_calls, final_output, latency_ms, model_name, output_format, fallback_used, error=None, tokens_in=None, tokens_out=None):
    """Insere registro estruturado em bot_turns. Falha silenciosa — não bloqueia resposta."""
    payload = {
        "user_id": user_id,
        "user_input": user_input,
        "tool_calls": tool_calls,
        "final_output": final_output,
        "latency_ms": latency_ms,
        "model": model_name,
        "output_format": output_format,
        "fallback_used": fallback_used,
        "error": error,
    }
    if tokens_in is not None:
        payload["tokens_in"] = tokens_in
    if tokens_out is not None:
        payload["tokens_out"] = tokens_out
    if tokens_in is not None and tokens_out is not None:
        payload["tokens_total"] = tokens_in + tokens_out
    try:
        supabase.table("bot_turns").insert(payload).execute()
    except Exception as e:
        # Migration 0008 (tokens_*) pode ainda não estar aplicada — fallback sem tokens
        if "tokens_" in str(e):
            try:
                for k in ("tokens_in", "tokens_out", "tokens_total"):
                    payload.pop(k, None)
                supabase.table("bot_turns").insert(payload).execute()
                return
            except Exception as e2:
                print(f"log_turn falhou (sem tokens): {e2}")
                return
        print(f"log_turn falhou: {e}")

# ================= NOVA FERRAMENTA DE BUSCA (SUPABASE) =================

def consultar_estoque_supabase(termo_cliente: str, tamanho: str = None, id_loja: str = None):
    """
    Realiza busca semântica no estoque por similaridade vetorial + filtro de tamanho + filtro de loja.

    Args:
        termo_cliente: o que o cliente busca (ex: "fantasia", "camisola algodão", "cueca boxer").
        tamanho: tamanho EXATO que o cliente pediu. Copie literal: se cliente disse "G",
                 passe "G" (NÃO "GG"). Se disse "M", passe "M". Se disse "42", passe "42".
                 Nunca expanda uma letra (G≠GG, M≠MM, P≠PP). Apenas colapse digitação
                 repetida em ≥4 (GGGG → GG). Use None só se o cliente realmente não falou
                 tamanho (ex: produtos sexshop, cosméticos).
        id_loja: id da loja a filtrar (string). Use o id_loja informado nas instruções
                 do prompt para restringir a busca a uma filial específica.
                 Use None para buscar em todas as lojas.

    Returns:
        dict com `status` ("sucesso" | "vazio" | "erro") e `filtro_aplicado`.
        `filtro_aplicado` é a VERDADE sobre o que foi consultado — vale acima do que
        você lembra de ter pedido. Se `filtro_aplicado.tamanho` for diferente do
        tamanho que você passou, a busca foi feita no valor de `filtro_aplicado`.
        Nunca afirme nada sobre um tamanho que não esteja em `filtro_aplicado`.
        Em "sucesso" vem `produtos` (lista). Cada item traz seu próprio `tamanho`,
        que pode ser COMPOSTO ('P/M', 'G/GG') ou acentuado ('ÚNICO') e ainda assim
        atender ao tamanho pedido — leia o campo do item; não diga que a peça é do
        tamanho X. Pode vir também `aviso`: instrução interna, nunca repita ao cliente.
    """
    print(f"\n[SEMÂNTICO] Buscando: '{termo_cliente}' | Tamanho recebido: {tamanho} | Loja: {id_loja}")

    # 0. Normalização do Tamanho
    tamanho_llm = tamanho.upper().strip() if tamanho else None
    tamanho_alvo = tamanho_llm

    # 0.1. DEFESA contra LLM drift (Risco B4): se cliente disse uma letra única
    # (ex: "G") e LLM passou outra (ex: "GG"), preferimos o que o cliente disse.
    tamanho_alvo, guard_acionou = _resolver_tamanho_alvo(tamanho_alvo)

    # 0.2. Estado do filtro — calculado SEMPRE (mesmo sem tamanho), porque o modo de
    # falha mais provável é o LLM OMITIR o tamanho que o cliente deu; sem isto ele
    # ficaria invisível. `tokens_alvo` é o mesmo overlap que a RPC faz (0009:55).
    tokens_user_log = _tokens_tamanho_do_cliente()
    llm_omitiu_tamanho = bool(tokens_user_log) and not tamanho_alvo
    tokens_alvo = set(_tokens_tamanho(tamanho_alvo)) if tamanho_alvo else set()
    # hoistado: id_loja_alvo entra no retorno/log dos 4 caminhos, inclusive o
    # early-return de embedding abaixo.
    id_loja_alvo = str(id_loja).strip() if id_loja not in (None, "") else None

    # 1. Gera o vetor da pergunta do cliente
    vetor_busca = get_embedding(termo_cliente)
    if not vetor_busca:
        return {"status": "erro", "msg": "Falha na geração do vetor de busca."}

    # 2. Chama a RPC 'buscar_produtos_semantico' no Supabase
    try:
        rpc_params = {
            'query_embedding': vetor_busca,
            'match_threshold': 0.5,
            'match_count': 10,
            'filtro_tamanho': tamanho_alvo,
            'filtro_id_loja': id_loja_alvo
        }
        response = supabase.rpc('buscar_produtos_semantico', rpc_params).execute()
        produtos_candidatos = response.data

        # OTIMIZAÇÃO: Remove o vetor de embedding (muito grande e inútil para a LLM) para economizar tokens
        for p in produtos_candidatos:
            p.pop('embedding', None)

        # 2.1. HYBRID SEARCH (KEYWORD BOOSTING)
        # Prioriza produtos que tenham a palavra exata no nome (ex: "RENDA")
        try:
            # Normalização de Plurais (Simples): Adiciona versão sem "S" final
            palavras_originais = [palavra.upper() for palavra in termo_cliente.split() if len(palavra) > 2]
            palavras_chave = set()
            for p in palavras_originais:
                palavras_chave.add(p)
                if p.endswith('S'):
                    palavras_chave.add(p[:-1]) # "CUECAS" -> "CUECA"

            GRUPO_PRIORITARIO = "PRODUTOS MAIS VENDIDOS"
            for p in produtos_candidatos:
                score_boost = 0
                nome_prod = p.get('nome', '').upper()
                for termo in palavras_chave:
                    if termo in nome_prod:
                        score_boost += 1
                # Boost comercial: bestsellers sempre acima de matches por palavra-chave
                if (p.get('nome_grupo') or '').upper() == GRUPO_PRIORITARIO:
                    score_boost += 10
                p['_score_boost'] = score_boost

            # Ordena: Quem tem mais palavras chave vai pro topo.
            # O Python's sort é estável, então se empatar no boost, mantém a ordem original (semântica)
            produtos_candidatos.sort(key=lambda x: x['_score_boost'], reverse=True)
            print(f"[HYBRID] Reordenado! Palavras-chave usadas: {palavras_chave}")

        except Exception as e:
            print(f"⚠️ Erro no Hybrid Boosting: {e}")

        # 2.2. BUSCA DE IMAGENS (NOVO)
        # Para cada produto candidato, buscar a imagem associada
        ids_candidatos = [p['id_produto'] for p in produtos_candidatos]
        if ids_candidatos:
            try:
                # Busca imagens onde produto_id está na lista de candidatos
                # Traz apenas 1 imagem por produto por enquanto (ou todas e a gente filtra)
                # O ideal seria um dicionário {id_produto: url}
                res_imgs = supabase.table("produtos_imagens").select("produto_id, imagem_url, imagem_mini_url").in_("produto_id", ids_candidatos).execute()
                mapa_imagens = {}
                for img in res_imgs.data:
                    pid = str(img['produto_id']) # Force string key
                    # Prioriza imagem original, depois mini. E apenas a primeira encontrada para cada produto
                    if pid not in mapa_imagens:
                        mapa_imagens[pid] = img.get('imagem_url') or img.get('imagem_mini_url')
                
                # Anexa a imagem ao objeto do produto
                for p in produtos_candidatos:
                    p['imagem'] = mapa_imagens.get(str(p['id_produto'])) # Force string lookup
            except Exception as e:
                print(f"⚠️ Erro ao buscar imagens: {e}")
        
        for i, p in enumerate(produtos_candidatos):
            print(f"  {i+1}. {p.get('nome')} | T:{p.get('tamanho')} | R$ {p.get('preco')}")
    except Exception as e:
        print(f"❌ Erro RPC: {e}")
        return {"status": "erro", "msg": "Erro ao consultar banco de dados."}

    # 3. Re-filtro por OVERLAP DE TOKENS — mesma regra da RPC (0009:55,
    # `tamanho_tokens && tokens_alvo`). A igualdade exata que existia aqui derrubava
    # o que o banco havia aprovado: 'ÚNICO'.upper() != 'UNICO' matava 623 das 674
    # linhas (92,4%), e todo tamanho COMPOSTO ('P/M' 20 linhas, 'G/GG' 18) era
    # invisível. Era essa a origem do falso "não tenho nesse tamanho".
    # `not tokens_alvo` espelha o branch cardinality(...)=0 da RPC.
    # G ≠ GG continua valendo (conjuntos disjuntos): a expansão que o prompt proíbe
    # não volta.
    validados = []
    dropados = []                      # tamanhos crus descartados pelo overlap
    n_dropados_igualdade_legado = 0    # sobreviveram ao overlap, morreriam na igualdade

    for p in produtos_candidatos:
        if tamanho_alvo:
            tokens_p = set(_tokens_tamanho(p.get('tamanho')))
            if (not tokens_alvo) or (tokens_alvo & tokens_p):
                validados.append(p)
                if (p.get('tamanho') or '').upper() != tamanho_alvo:
                    n_dropados_igualdade_legado += 1
            else:
                dropados.append(p.get('tamanho'))
        else:
            # Para cosméticos/acessórios, aceitamos o que vier com maior similaridade
            validados.append(p)

    if n_dropados_igualdade_legado:
        print(f"[TAMANHO] overlap salvou {n_dropados_igualdade_legado} linha(s) que a "
              f"igualdade exata derrubaria (alvo={tamanho_alvo})")
    if dropados:
        print(f"[TAMANHO] descartados por tamanho: {dropados}")

    # 3.1. O retorno DECLARA o filtro aplicado. O modelo tratava a própria memória do
    # que pediu como verdade; agora a verdade vem no payload. `aviso` só existe quando
    # o guard corrigiu — nunca mandar chave None ao Gemini.
    filtro_aplicado = {
        "tamanho": tamanho_alvo,
        "tamanho_tokens": sorted(tokens_alvo),
        "id_loja": id_loja_alvo,
        "observacao_tamanho": (
            "Cada item traz seu proprio campo 'tamanho'. Ele pode ser COMPOSTO "
            "('P/M','G/GG') ou acentuado ('UNICO'/'ÚNICO') e ainda assim atender o "
            "tamanho pedido. Nunca afirme que um item e do tamanho X: leia o campo "
            "'tamanho' do item."),
    }
    # O texto NAO manda re-chamar a tool com outro tamanho: o guard lê a mensagem do
    # cliente, que não muda dentro do turno, então a re-chamada seria corrigida de novo
    # e o modelo entraria em loop. E não afirma "todos são do tamanho X", que seria
    # falso depois do overlap — justamente a mentira que este campo existe para matar.
    aviso = (f"INSTRUCAO INTERNA (nao repita ao cliente): a ferramenta foi chamada com "
             f"tamanho '{tamanho_llm}', mas nesta mensagem o cliente escreveu "
             f"{tokens_user_log}. A busca foi executada LITERALMENTE em '{tamanho_alvo}'. "
             f"Nao afirme nada sobre '{tamanho_llm}'. A ferramenta so aceita o tamanho que "
             f"o cliente escreveu nesta mensagem — se precisar de outro tamanho, PERGUNTE "
             f"ao cliente.")

    if not validados:
        return {"status": "vazio", "filtro_aplicado": filtro_aplicado,
                **({"aviso": aviso} if guard_acionou else {}),
                "msg": (f"Nenhum produto com estoque para o termo buscado no tamanho "
                        f"{tamanho_alvo} na loja {id_loja_alvo}. O filtro foi aplicado "
                        f"literalmente; nao conclua nada sobre outro tamanho sem chamar "
                        f"a ferramenta de novo.")}

    # 4. Top-K final preservando ordem do boost (set destruía ordenação)
    vistos = set()
    selecao = []
    for p in validados[:10]:
        chave = p.get('id_unico')
        if chave and chave not in vistos:
            vistos.add(chave)
            selecao.append(p)

    # Sinal explícito de foto p/ o modelo: evita prometer imagem que o sistema
    # não vai enviar (card sem imagem sai só como texto — renderizar_mensagem_estruturada).
    for p in selecao:
        p['tem_foto'] = bool(p.get('imagem'))

    print(f"[SEMÂNTICO] Retornando {len(selecao)} itens.")
    return {"status": "sucesso", "filtro_aplicado": filtro_aplicado,
            **({"aviso": aviso} if guard_acionou else {}),
            "produtos": selecao}

# ================= FERRAMENTAS DE CONSULTA DETERMINÍSTICA =================

def consultar_produto_por_id(id_produto: int):
    """
    Lê o produto direto do banco por id, com todas as variações ativas (estoque > 0).

    Use esta ferramenta SEMPRE que precisar revalidar preço, estoque ou tamanhos
    disponíveis de um produto que já apareceu na conversa antes de citar
    valor ao cliente. Ela é a fonte da verdade — não confie em preços
    lembrados de turnos anteriores.

    Args:
        id_produto: o id numérico do produto (campo id_produto da tabela produtos_estoque).

    Returns:
        {status, produto: {id_produto, nome, nome_grupo, variacoes: [{id_unico, tamanho, preco_varejo, preco_atacado, estoque, loja}]}}
    """
    # Gemini eventualmente passa id_produto como float (54557957.0). Cast para int
    # antes de virar string para evitar mismatch com '54557957' no banco.
    try:
        id_produto_normalizado = str(int(float(id_produto)))
    except (TypeError, ValueError):
        return {"status": "erro", "msg": f"id_produto invalido: {id_produto!r}"}

    try:
        resp = (
            supabase.table("produtos_estoque")
            .select("id_unico, id_produto, id_loja, loja, nome, tamanho, preco_varejo, preco_atacado, estoque, grupo_id, nome_grupo")
            .eq("id_produto", id_produto_normalizado)
            .gt("estoque", 0)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        print(f"❌ Erro consultar_produto_por_id({id_produto}): {e}")
        return {"status": "erro", "msg": "Erro ao consultar produto."}

    if not rows:
        return {"status": "nao_encontrado", "msg": f"Produto {id_produto} sem estoque ou inexistente."}

    base = rows[0]
    variacoes = [
        {
            "id_unico": r["id_unico"],
            "tamanho": r.get("tamanho"),
            "preco_varejo": float(r.get("preco_varejo") or 0),
            "preco_atacado": float(r.get("preco_atacado") or 0),
            "estoque": float(r.get("estoque") or 0),
            "loja": r.get("loja"),
        }
        for r in rows
    ]
    return {
        "status": "sucesso",
        "produto": {
            "id_produto": base["id_produto"],
            "nome": base.get("nome"),
            "nome_grupo": base.get("nome_grupo"),
            "variacoes": variacoes,
        },
    }


# Defaults caso bot_settings não tenha configurado os parâmetros de atacado.
_ATACADO_DEFAULT = {
    "desconto_avista": 0.30,
    "desconto_aprazo": 0.25,
    "minimo_primeira_compra": 600.0,
    "minimo_proxima_compra": 400.0,
    "parcelas_max": 6,
}


def _ler_config_atacado():
    """Lê parâmetros de atacado de bot_settings com fallback para defaults."""
    try:
        cfg = supabase.table("bot_settings").select(
            "atacado_desconto_avista, atacado_desconto_aprazo, atacado_minimo_primeira, atacado_minimo_proxima, atacado_parcelas_max"
        ).eq("id", 1).single().execute()
        d = cfg.data or {}
    except Exception:
        d = {}
    return {
        "desconto_avista": float(d.get("atacado_desconto_avista") or _ATACADO_DEFAULT["desconto_avista"]),
        "desconto_aprazo": float(d.get("atacado_desconto_aprazo") or _ATACADO_DEFAULT["desconto_aprazo"]),
        "minimo_primeira_compra": float(d.get("atacado_minimo_primeira") or _ATACADO_DEFAULT["minimo_primeira_compra"]),
        "minimo_proxima_compra": float(d.get("atacado_minimo_proxima") or _ATACADO_DEFAULT["minimo_proxima_compra"]),
        "parcelas_max": int(d.get("atacado_parcelas_max") or _ATACADO_DEFAULT["parcelas_max"]),
    }


def calcular_total(itens_json: str, modo: str = "varejo", primeira_compra: bool = False):
    """
    Calcula o total de um carrinho, com desconto correto por modo e validação de mínimo.

    Use esta ferramenta SEMPRE antes de citar valor agregado, total, desconto
    ou parcelado para o cliente. Não calcule de cabeça — chame aqui.

    Args:
        itens_json: JSON string de uma lista de itens, no formato
                    '[{"id_produto": 123, "qtd": 2}, {"id_produto": 456, "qtd": 1}]'.
                    Use os id_produto retornados por consultar_estoque_supabase.
        modo: "varejo" | "atacado_avista" | "atacado_aprazo".
              "varejo" = preço cheio. "atacado_avista" = preço de atacado em pix/dinheiro.
              "atacado_aprazo" = preço de atacado parcelado.
        primeira_compra: True se é a primeira compra do cliente no atacado
                         (mínimo R$ 600). False se já comprou antes (mínimo R$ 400).
                         Ignorado em modo varejo.

    Returns:
        {status, subtotal_varejo, total, desconto, minimo_exigido, minimo_atingido,
         falta_para_minimo, parcelado_6x, itens_detalhados: [...]}
    """
    try:
        itens = json.loads(itens_json) if isinstance(itens_json, str) else itens_json
        if not isinstance(itens, list) or not itens:
            return {"status": "erro", "msg": "itens_json deve ser uma lista não vazia."}
    except json.JSONDecodeError as e:
        return {"status": "erro", "msg": f"itens_json inválido: {e}"}

    ids = []
    qtd_por_id = {}
    for item in itens:
        try:
            pid = int(item["id_produto"])
            qtd = max(1, int(item.get("qtd", 1)))
            ids.append(pid)
            qtd_por_id[pid] = qtd_por_id.get(pid, 0) + qtd
        except (KeyError, TypeError, ValueError):
            return {"status": "erro", "msg": f"item inválido: {item}. Esperado {{id_produto, qtd}}."}

    if modo not in ("varejo", "atacado_avista", "atacado_aprazo"):
        return {"status": "erro", "msg": f"modo inválido: {modo}"}

    try:
        resp = (
            supabase.table("produtos_estoque")
            .select("id_produto, nome, preco_varejo, preco_atacado, preco_atacado_aprazo")
            .in_("id_produto", [str(i) for i in ids])
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        print(f"❌ Erro calcular_total: {e}")
        return {"status": "erro", "msg": "Erro ao consultar produtos."}

    precos = {}
    for r in rows:
        pid = int(r["id_produto"])
        if pid not in precos:
            precos[pid] = {
                "nome": r.get("nome"),
                "preco_varejo": float(r.get("preco_varejo") or 0),
                "preco_atacado": float(r.get("preco_atacado") or 0),
                "preco_atacado_aprazo": float(r.get("preco_atacado_aprazo") or 0),
            }

    cfg = _ler_config_atacado()
    subtotal_varejo = 0.0
    subtotal_atacado_avista = 0.0
    subtotal_atacado_aprazo = 0.0
    detalhes = []
    nao_encontrados = []

    for pid, qtd in qtd_por_id.items():
        info = precos.get(pid)
        if not info:
            nao_encontrados.append(pid)
            continue
        pv = info["preco_varejo"]
        # Atacado à vista: lê do DB. Se ausente ou igual ao varejo (cadastro
        # incompleto no ERP), aplica desconto configurável.
        pa_raw = info["preco_atacado"]
        pa = pa_raw if pa_raw and pa_raw < pv else pv * (1 - cfg["desconto_avista"])
        # Atacado a prazo: lê do DB. Mesmo critério de fallback.
        pap_raw = info["preco_atacado_aprazo"]
        pap = pap_raw if pap_raw and pap_raw < pv else pv * (1 - cfg["desconto_aprazo"])

        subtotal_varejo += pv * qtd
        subtotal_atacado_avista += pa * qtd
        subtotal_atacado_aprazo += pap * qtd
        detalhes.append({
            "id_produto": pid,
            "nome": info["nome"],
            "qtd": qtd,
            "preco_varejo_unit": round(pv, 2),
            "preco_atacado_avista_unit": round(pa, 2),
            "preco_atacado_aprazo_unit": round(pap, 2),
        })

    if nao_encontrados:
        return {"status": "erro", "msg": f"id_produto não encontrado(s): {nao_encontrados}"}

    if modo == "varejo":
        total = subtotal_varejo
        minimo = 0.0
    elif modo == "atacado_avista":
        total = subtotal_atacado_avista
        minimo = cfg["minimo_primeira_compra"] if primeira_compra else cfg["minimo_proxima_compra"]
    else:  # atacado_aprazo
        total = subtotal_atacado_aprazo
        minimo = cfg["minimo_primeira_compra"] if primeira_compra else cfg["minimo_proxima_compra"]

    desconto = subtotal_varejo - total if modo != "varejo" else 0.0
    minimo_atingido = total >= minimo if minimo > 0 else True
    falta = max(0.0, minimo - total) if minimo > 0 else 0.0
    parcela = total / cfg["parcelas_max"] if cfg["parcelas_max"] > 0 else total

    return {
        "status": "sucesso",
        "modo": modo,
        "primeira_compra": primeira_compra,
        "subtotal_varejo": round(subtotal_varejo, 2),
        "total": round(total, 2),
        "desconto": round(desconto, 2),
        "minimo_exigido": round(minimo, 2),
        "minimo_atingido": minimo_atingido,
        "falta_para_minimo": round(falta, 2),
        "parcelado": {"parcelas": cfg["parcelas_max"], "valor_parcela": round(parcela, 2)} if modo == "atacado_aprazo" else None,
        "itens_detalhados": detalhes,
    }


def verificar_promocao_hoje():
    """
    Verifica se há promoção ativa hoje (Dia S e similares).

    Use esta ferramenta SEMPRE antes de mencionar Dia S, desconto promocional
    ou qualquer oferta atrelada a dia da semana. Nunca afirme que existe
    promoção sem chamar essa tool primeiro.

    Lê da view `vw_promocao_ativa_hoje` que prioriza agendamentos específicos
    em `dia_s_calendario` sobre regras semanais em `promocoes_ativas`. A view
    já calcula o dia da semana em America/Sao_Paulo.

    Returns:
        {status, hoje_dia_semana, promocoes: [{fonte, categoria, percentual, formas_pagamento, observacao, dia_semana}]}
    """
    try:
        from datetime import datetime, timezone, timedelta
        agora = datetime.now(timezone(timedelta(hours=-3)))
        dow = (agora.weekday() + 1) % 7  # apenas para o retorno; filtro feito pela view

        resp = (
            supabase.from_("vw_promocao_ativa_hoje")
            .select("fonte, categoria, percentual, formas_pagamento, observacao, dia_semana")
            .execute()
        )
        promos = resp.data or []
    except Exception as e:
        print(f"Erro verificar_promocao_hoje: {e}")
        return {"status": "erro", "msg": "Erro ao consultar promoções."}

    return {
        "status": "sucesso",
        "hoje_dia_semana": dow,
        "promocoes": promos,
    }


# ================= FERRAMENTA: CALCULAR FRETE ESTIMADO =================

# (keywords, peso_g, altura_cm, largura_cm, comprimento_cm)
_PESO_TABLE = [
    (["cueca", "boxer", "slip"],             80,  3, 20, 15),
    (["calcinha", "fio dental", "tanga"],    60,  2, 18, 12),
    (["sutiã", "sutia", "top", "bralette"], 120,  4, 25, 20),
    (["conjunto"],                           200,  5, 25, 20),
    (["body", "bodysuit"],                   200,  5, 28, 22),
    (["legging", "calça"],                   350,  5, 30, 25),
    (["meia", "soquete"],                     60,  3, 15, 10),
    (["camisola", "camiseta"],               250,  4, 30, 25),
]
_PESO_DEFAULT = (150, 4, 22, 18)


def _estimar_pacote(nome_produto: str, quantidade: int = 1):
    nome_lower = nome_produto.lower()
    peso_u, h, w, l = _PESO_DEFAULT
    for keywords, pw, ph, pw2, pl in _PESO_TABLE:
        if any(kw in nome_lower for kw in keywords):
            peso_u, h, w, l = pw, ph, pw2, pl
            break
    peso_total = peso_u * max(quantidade, 1)
    if quantidade > 1:
        h = h + (quantidade - 1) * 2
        w = w + (quantidade - 1) * 1
    # Melhor Envio mínimos: 11x16x2cm, 300g
    return max(peso_total, 300), max(h, 2), max(w, 16), max(l, 11)


def _validar_cep(cep: str):
    limpo = re.sub(r'\D', '', cep or '')
    return limpo if len(limpo) == 8 else None


def _fallback_frete():
    return {
        "status": "indisponivel",
        "msg": "Não consegui calcular o frete agora. Nossa equipe confirma o valor exato na hora do envio. Posso te transferir para um atendente? 😊"
    }


def calcular_frete_estimado(
    cep_destino: str,
    id_produto: int = None,
    quantidade: int = 1,
    nome_produto: str = None,
    itens_json: str = None,
):
    """
    Estima o custo de frete da Sangali até o CEP do cliente.

    Use esta ferramenta quando o cliente perguntar sobre frete, entrega, prazo ou custo de envio.
    IMPORTANTE: Chame esta ferramenta SOMENTE após o cliente informar o CEP.

    Para carrinho com múltiplos produtos, use `itens_json`. Para um único produto,
    use `id_produto`. `nome_produto` é fallback textual.

    Args:
        cep_destino:  CEP do cliente (ex: "29900-161" ou "29900161").
        id_produto:   id numérico do produto principal (preferido para 1 item).
        quantidade:   Quantidade quando usa id_produto (padrão 1, máx 50).
        nome_produto: Fallback textual quando id_produto desconhecido.
        itens_json:   JSON string com lista de itens para carrinho:
                      '[{"id_produto":123,"qtd":2},{"id_produto":456,"qtd":1}]'.
                      Quando preenchido, soma os pesos dos itens.
    """
    cep_limpo = _validar_cep(cep_destino)
    if not cep_limpo:
        return {"status": "erro_cep", "msg": "CEP inválido. Informe o CEP com 8 dígitos, ex: 87013-000."}

    # Modo carrinho: itens_json tem prioridade se preenchido
    pacote = None
    nome_para_peso = ""
    qtd_total = 0

    if itens_json:
        try:
            itens = json.loads(itens_json) if isinstance(itens_json, str) else itens_json
            if not isinstance(itens, list) or not itens:
                return {"status": "erro", "msg": "itens_json deve ser lista não vazia."}
            ids = [str(it["id_produto"]) for it in itens]
            r = supabase.table("produtos_estoque").select("id_produto, nome, nome_grupo").in_("id_produto", ids).execute()
            mapa_nome = {row["id_produto"]: (row.get("nome") or "") + " " + (row.get("nome_grupo") or "")
                         for row in (r.data or [])}
            peso_total = 0
            altura_total = 0
            largura_max = 0
            comp_max = 0
            for it in itens:
                pid = str(it["id_produto"])
                qtd = max(1, min(50, int(it.get("qtd", 1))))
                qtd_total += qtd
                nome_item = mapa_nome.get(pid, "")
                nome_para_peso += " " + nome_item
                p, a, l, c = _estimar_pacote(nome_item, qtd)
                peso_total += p
                altura_total += a  # empilha
                largura_max = max(largura_max, l)
                comp_max = max(comp_max, c)
            pacote = (peso_total, max(altura_total, 2), max(largura_max, 16), max(comp_max, 11))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return {"status": "erro", "msg": f"itens_json inválido: {e}"}

    if pacote is None:
        # Modo single (legado / um produto)
        try:
            quantidade = max(1, min(50, int(quantidade)))
        except (TypeError, ValueError):
            quantidade = 1
        qtd_total = quantidade

        if id_produto:
            try:
                r = supabase.table("produtos_estoque").select("nome, nome_grupo").eq("id_produto", str(id_produto)).limit(1).execute()
                if r.data:
                    nome_para_peso = (r.data[0].get("nome") or "") + " " + (r.data[0].get("nome_grupo") or "")
            except Exception as e:
                print(f"Frete: lookup id_produto={id_produto} falhou: {e}")

        if not nome_para_peso:
            nome_para_peso = nome_produto or ""

        if not nome_para_peso:
            return {"status": "erro", "msg": "Informe id_produto, itens_json ou nome_produto."}

        pacote = _estimar_pacote(nome_para_peso, quantidade)

    peso_g, alt, larg, comp = pacote

    try:
        cfg = supabase.table("bot_settings").select("cep_origem").eq("id", 1).single().execute()
        cep_origem = _validar_cep((cfg.data or {}).get("cep_origem", ""))
    except Exception as e:
        print(f"Frete: erro ao buscar cep_origem: {e}")
        cep_origem = None

    if not cep_origem:
        return {"status": "erro_config", "msg": "Configuração da loja ausente. Transfira para atendente."}

    print(f"Frete: cep={cep_limpo} | itens={qtd_total} | nome='{nome_para_peso[:40]}' | pacote={peso_g}g {alt}x{larg}x{comp}cm")

    payload = {
        "from": {"postal_code": cep_origem},
        "to":   {"postal_code": cep_limpo},
        "package": {
            "height": alt,
            "width":  larg,
            "length": comp,
            "weight": round(peso_g / 1000, 3)
        },
        "options": {"insurance_value": 0, "receipt": False, "own_hand": False},
        "services": ""
    }
    headers = {
        "Authorization": f"Bearer {MELHOR_ENVIO_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "SangaliBot/1.0 (contato@sangali.com.br)"
    }

    try:
        resp = requests.post(MELHOR_ENVIO_URL, json=payload, headers=headers, timeout=10)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"⚠️ Frete: falha de conexão: {e}")
        return _fallback_frete()

    if resp.status_code != 200:
        print(f"⚠️ Frete: status {resp.status_code} | {resp.text[:200]}")
        return _fallback_frete()

    try:
        servicos = resp.json()
    except Exception:
        return _fallback_frete()

    opcoes = [
        {
            "servico":    s.get("name", ""),
            "preco":      float(s["price"]),
            "prazo_dias": s.get("delivery_time")
        }
        for s in servicos
        if not s.get("error") and s.get("price") is not None
    ]

    if not opcoes:
        return _fallback_frete()

    opcoes.sort(key=lambda x: x["preco"])
    print(f"✅ Frete: {len(opcoes)} opções encontradas para {cep_limpo}")

    return {
        "status": "sucesso",
        "cep_destino": f"{cep_limpo[:5]}-{cep_limpo[5:]}",
        "opcoes": opcoes[:5],
        "disclaimer": "⚠️ Valor ESTIMADO. O frete exato é calculado após pesagem real do pacote no momento do despacho."
    }


# ================= HANDOFF PARA ATENDENTE HUMANO =================

def _montar_mensagem_operador(user_id, motivo, resumo, produtos_interesse):
    linhas = [
        "🔔 Transferência — Sangali Bot",
        "",
        f"Cliente: {user_id}",
        f"Motivo: {motivo}",
    ]
    if produtos_interesse:
        linhas.append(f"Produtos de interesse: {produtos_interesse}")
    linhas += ["", "Resumo:", resumo, "", "Últimas mensagens:"]

    try:
        historico = get_history(user_id, limit=8)
        for msg in historico:
            quem = "cliente" if msg["role"] == "user" else "bot"
            texto = msg["parts"][0] if msg.get("parts") else ""
            texto_curto = (texto[:200] + "…") if len(texto) > 200 else texto
            linhas.append(f"[{quem}] {texto_curto}")
    except Exception as e:
        print(f"⚠️ Erro ao montar histórico para operador: {e}")

    return "\n".join(linhas)


def criar_tool_transferir(user_id):
    """Retorna a função tool amarrada ao user_id da conversa atual."""

    def transferir_para_atendente(motivo: str, resumo: str, produtos_interesse: str = ""):
        """
        Aciona a transferência da conversa para um atendente humano.

        Use quando:
        (1) o cliente pedir explicitamente para falar com um humano/atendente/vendedor;
        (2) a venda estiver quase fechando e for necessário confirmar uma variação
            que o bot não tem (ex: cor);
        (3) o cliente demonstrar irritação clara ou houver confusão repetida
            (2 ou mais mal-entendidos seguidos).

        Args:
            motivo: curto e categórico. Ex: "fechamento_venda", "pedido_humano",
                    "confusao_repetida".
            resumo: 2 a 4 frases descrevendo o que aconteceu na conversa e o que
                    o atendente precisa saber para continuar.
            produtos_interesse: nomes/códigos dos produtos mencionados, separados
                                por vírgula. Vazio se não aplicável.
        """
        # Idempotência: se já houve handoff nos últimos HANDOFF_SILENCIO_MIN minutos
        # para este user, não duplica WhatsApp nem row em conversation_handoffs.
        delta = _ultimo_handoff_em(user_id)
        if delta is not None and delta < timedelta(minutes=HANDOFF_SILENCIO_MIN):
            print(f"🔁 Handoff já ativo há {delta} para {user_id} — disparo suprimido.")
            return {"status": "ja_transferido", "msg": "Conversa já está com atendente."}

        try:
            config = supabase.table("bot_settings").select("operator_number").eq("id", 1).single().execute()
            operator_number = (config.data or {}).get("operator_number") if config else None
        except Exception as e:
            print(f"❌ Erro ao ler operator_number: {e}")
            return {"status": "erro", "msg": "Configuração do atendente não encontrada."}

        if not operator_number:
            print("⚠️ operator_number não configurado em bot_settings.")
            return {"status": "erro", "msg": "Número do atendente não configurado."}

        texto = _montar_mensagem_operador(user_id, motivo, resumo, produtos_interesse)
        enviar_mensagem_whatsapp(operator_number, texto)

        try:
            supabase.table("conversation_handoffs").insert({
                "user_id": user_id,
                "motivo": motivo,
                "resumo": resumo,
                "produtos_interesse": produtos_interesse,
                "operator_number": operator_number,
            }).execute()
        except Exception as e:
            print(f"⚠️ Erro ao registrar handoff: {e}")

        print(f"🤝 Handoff disparado para {operator_number} | user={user_id} | motivo={motivo}")
        return {"status": "ok"}

    return transferir_para_atendente


# ================= CONFIGURAÇÃO DA IA (MODELO) =================

# Schema da resposta final do LLM. resposta_texto NÃO deve conter URLs;
# o código monta as cards de produto a partir de produtos_recomendados,
# usando dados canônicos retornados pelas tools no mesmo turno.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "resposta_texto": {"type": "string"},
        "produtos_recomendados": {
            "type": "array",
            "items": {"type": "integer"},
        },
        # Contexto de preco dos cards deste turno. "varejo" (default) = card mostra
        # preco cheio; "atacado_avista"/"atacado_aprazo" = card mostra o preco de
        # atacado com o varejo como referencia. O modelo seta conforme a conversa.
        "modo_preco": {
            "type": "string",
            "enum": ["varejo", "atacado_avista", "atacado_aprazo"],
        },
    },
    "required": ["resposta_texto"],
}

GENERATION_CONFIG = {
    "response_mime_type": "application/json",
    "response_schema": RESPONSE_SCHEMA,
}


def _coerce_to_dict(obj):
    """Converte struct/proto/dict-like em estrutura JSON-serializável (recursivo).

    Lida com:
    - dicts (recursão nos valores)
    - listas/tuples (recursão nos itens)
    - proto messages (via _pb e MessageToDict)
    - proto MapComposite e MapField (iteráveis dict-like)
    - tipos primitivos
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _coerce_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce_to_dict(v) for v in obj]
    # Proto MapComposite, MapField — iterável dict-like
    try:
        from google.protobuf.json_format import MessageToDict
        if hasattr(obj, '_pb'):
            return _coerce_to_dict(MessageToDict(obj._pb, preserving_proto_field_name=True))
    except Exception:
        pass
    # Tenta tratar como dict (proto MapComposite suporta .items())
    try:
        if hasattr(obj, 'items'):
            return {k: _coerce_to_dict(v) for k, v in obj.items()}
    except Exception:
        pass
    # Tenta tratar como iterável (proto RepeatedComposite / lista)
    try:
        if hasattr(obj, '__iter__') and not isinstance(obj, (bytes, bytearray)):
            return [_coerce_to_dict(v) for v in obj]
    except Exception:
        pass
    # Último recurso: string repr
    try:
        return str(obj)
    except Exception:
        return None


def extrair_produtos_de_tool_results(chat_history):
    """
    Indexa por id_produto o ÚLTIMO retorno de consultar_estoque_supabase
    e consultar_produto_por_id na history. Resultado: cache canônico
    pra renderização determinística da mensagem final.
    """
    cache = {}
    for content in chat_history or []:
        parts = getattr(content, 'parts', None) or []
        for part in parts:
            fr = getattr(part, 'function_response', None)
            if not fr:
                continue
            tool_name = getattr(fr, 'name', None)
            if tool_name not in ('consultar_estoque_supabase', 'consultar_produto_por_id'):
                continue
            response_dict = _coerce_to_dict(getattr(fr, 'response', None))
            if not response_dict:
                continue
            payload = response_dict.get('result') or response_dict
            if tool_name == 'consultar_estoque_supabase':
                for prod in payload.get('produtos', []) or []:
                    pid = prod.get('id_produto')
                    try:
                        key = int(pid)
                    except (TypeError, ValueError):
                        continue
                    cache[key] = {
                        "nome": prod.get('nome'),
                        "preco": prod.get('preco') or prod.get('preco_varejo'),
                        "preco_varejo": prod.get('preco_varejo'),
                        "preco_atacado": prod.get('preco_atacado'),
                        "imagem": prod.get('imagem'),
                        "tamanho": prod.get('tamanho'),
                        "id_unico": prod.get('id_unico'),
                    }
            elif tool_name == 'consultar_produto_por_id':
                produto = payload.get('produto') or {}
                pid = produto.get('id_produto')
                try:
                    key = int(pid)
                except (TypeError, ValueError):
                    continue
                variacoes = produto.get('variacoes') or []
                primeira = variacoes[0] if variacoes else {}
                cache[key] = {
                    "nome": produto.get('nome'),
                    "preco": primeira.get('preco_varejo'),
                    "preco_varejo": primeira.get('preco_varejo'),
                    "preco_atacado": primeira.get('preco_atacado'),
                    "imagem": None,
                    "tamanho": primeira.get('tamanho'),
                    "id_unico": primeira.get('id_unico'),
                }
    return cache


def serializar_tool_calls(chat_history):
    """Lista cronológica de tool calls com args + result (truncado) para logging."""
    chamadas = []
    for content in chat_history or []:
        parts = getattr(content, 'parts', None) or []
        for part in parts:
            fc = getattr(part, 'function_call', None)
            if fc and getattr(fc, 'name', None):
                chamadas.append({
                    "kind": "call",
                    "name": fc.name,
                    "args": _coerce_to_dict(getattr(fc, 'args', None)) or {},
                })
            fr = getattr(part, 'function_response', None)
            if fr and getattr(fr, 'name', None):
                resp = _coerce_to_dict(getattr(fr, 'response', None)) or {}
                resp_str = json.dumps(resp, ensure_ascii=False, default=str)
                # 4000, nao 2000: `filtro_aplicado` + `aviso` entram ANTES de `produtos`
                # e comiam o orcamento do digest de que o harness depende para extrair
                # id_produto (run_eval.py:_prod_ids_from_responses). E log em bot_turns,
                # nao contexto do Gemini: o custo e armazenamento.
                if len(resp_str) > 4000:
                    resp_str = resp_str[:4000] + "...(truncated)"
                chamadas.append({
                    "kind": "response",
                    "name": fr.name,
                    "result_digest": resp_str,
                })
    return chamadas


def _formatar_preco(valor):
    if valor is None:
        return "—"
    try:
        return f"R$ {float(valor):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return f"R$ {valor}"


def parsear_resposta_json(texto_bruto):
    """
    Tenta parsear como JSON do schema esperado.
    Retorna (resposta_texto, produtos_recomendados, json_ok, modo_preco).
    modo_preco in {"varejo","atacado_avista","atacado_aprazo"} (default "varejo").
    Em caso de falha, retorna (texto_bruto, [], False, "varejo") e o caller cai no fallback regex.
    """
    if not texto_bruto:
        return "", [], False, "varejo"
    texto = texto_bruto.strip()
    # Gemini pode envolver em ```json ... ``` se o response_schema for ignorado.
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(texto)
    except json.JSONDecodeError:
        return texto_bruto, [], False, "varejo"
    if not isinstance(obj, dict):
        return texto_bruto, [], False, "varejo"
    resposta = obj.get("resposta_texto", "")
    ids = obj.get("produtos_recomendados") or []
    if not isinstance(ids, list):
        ids = []
    ids_int = []
    for i in ids:
        try:
            ids_int.append(int(i))
        except (TypeError, ValueError):
            pass
    modo_preco = obj.get("modo_preco") or "varejo"
    if modo_preco not in ("varejo", "atacado_avista", "atacado_aprazo"):
        modo_preco = "varejo"
    return str(resposta), ids_int, True, modo_preco


_ATACADO_KEYWORDS = ("atacado", "revend", "por atacado", "no atacado", "pra revenda", "para revenda")


def _detectar_atacado(texto):
    """Sinal deterministico de contexto de atacado no texto do cliente. Conservador
    (palavras que praticamente nao aparecem em conversa de varejo) p/ minimizar
    falso-positivo. So decide EXIBICAO de card — nunca o total (que vem do calcular_total)."""
    t = (texto or "").lower()
    return any(k in t for k in _ATACADO_KEYWORDS)


def _modo_preco_efetivo(user_id, modo_preco_modelo, texto_cliente):
    """Combina o modo_preco do modelo com o contexto de atacado detectado/lembrado.
    - Se o modelo marcou atacado, respeita (e memoriza o user como atacado).
    - Se detectou atacado no texto do cliente, memoriza.
    - Se o user esta em contexto de atacado mas o modelo deixou 'varejo', promove
      p/ 'atacado_avista' (default seguro; o modelo pode especificar aprazo)."""
    if modo_preco_modelo in ("atacado_avista", "atacado_aprazo"):
        _atacado_users.add(user_id)
        return modo_preco_modelo
    if _detectar_atacado(texto_cliente):
        _atacado_users.add(user_id)
    if user_id in _atacado_users:
        return "atacado_avista"
    return "varejo"


def _legenda_card(pid, info, modo_preco="varejo", cfg_atacado=None):
    """Monta a legenda do card. Em modo atacado mostra o preco de atacado
    (varejo * (1-desconto)) em destaque e o varejo como referencia; em varejo,
    mantem o formato `*Nome* - R$ Valor`. O `_cód: id_` e sempre preservado."""
    nome = info.get("nome") or f"Produto {pid}"
    preco = info.get("preco")
    if modo_preco in ("atacado_avista", "atacado_aprazo") and isinstance(preco, (int, float)):
        cfg = cfg_atacado or _ler_config_atacado()
        desc = cfg["desconto_avista"] if modo_preco == "atacado_avista" else cfg["desconto_aprazo"]
        rotulo = "à vista" if modo_preco == "atacado_avista" else "parcelado"
        atacado_unit = round(float(preco) * (1 - desc), 2)
        return (f"*{nome}*\n💵 Atacado {rotulo}: {_formatar_preco(atacado_unit)}/un\n"
                f"(de {_formatar_preco(preco)} no varejo)\n_cód: {int(pid)}_")
    return f"*{nome}* - {_formatar_preco(preco)}\n_cód: {int(pid)}_"


def renderizar_mensagem_estruturada(user_id, resposta_texto, ids_recomendados, cache, modo_preco="varejo"):
    """
    Caminho determinístico: envia resposta_texto seguido de cards
    `*Nome* - R$ Valor` + imagem por id, usando dados canônicos do cache.
    Em modo atacado (modo_preco), o card mostra o preco de atacado + varejo de referencia.
    Ids fora do cache do turno atual são ignorados (e logados).
    Em provider "cloud", captura o wamid de cada card e persiste wamid->id_produto
    (card_envios) p/ o reply-to-card (a Cloud API so devolve context.id, nao a legenda).
    """
    if resposta_texto and resposta_texto.strip():
        enviar_mensagem_whatsapp(user_id, resposta_texto.strip())

    cfg_atacado = _ler_config_atacado() if modo_preco in ("atacado_avista", "atacado_aprazo") else None
    enviados = 0
    ignorados = []
    for pid in ids_recomendados[:5]:  # hard cap
        info = cache.get(int(pid))
        if not info:
            ignorados.append(pid)
            continue
        legenda = _legenda_card(pid, info, modo_preco, cfg_atacado)
        url = info.get("imagem")
        if url:
            time.sleep(1.0)
            wamid = enviar_midia_whatsapp(user_id, url, legenda)
        else:
            time.sleep(0.5)
            wamid = enviar_mensagem_whatsapp(user_id, legenda)
        if WHATSAPP_PROVIDER == "cloud" and isinstance(wamid, str):
            registrar_card_enviado(wamid, user_id, int(pid), legenda)
        enviados += 1
    if ignorados:
        print(f"⚠️ ids fora do cache do turn: {ignorados}")
    return enviados, ignorados


def renderizar_mensagem_regex_fallback(user_id, texto):
    """Fallback: parsing antigo por regex em [IMAGEM:url]."""
    parts = re.split(r"\[IMAGEM:(.*?)\]", texto, flags=re.DOTALL)
    if len(parts) > 1:
        for i in range(0, len(parts) - 1, 2):
            texto_legenda = parts[i].strip()
            url_imagem = parts[i+1].strip()
            if url_imagem:
                enviar_midia_whatsapp(user_id, url_imagem, texto_legenda)
                time.sleep(1.5)
        ultimo_texto = parts[-1].strip()
        if ultimo_texto:
            enviar_mensagem_whatsapp(user_id, ultimo_texto)
    else:
        enviar_mensagem_whatsapp(user_id, texto)


# ================= LÓGICA DA CONVERSA & WEBHOOK =================

# Erros transitorios do Gemini (504/503/500/429) — valem retry; safety/blocked nao.
_IA_ERR_TRANSITORIO = (
    "deadline", "504", "503", "500", "unavailable", "resource exhausted",
    "resourceexhausted", "429", "internal error", "temporarily", "try again",
)


def _erro_ia_transitorio(e):
    """True se o erro do Gemini for transitorio (vale retry), False se for permanente
    (safety/blocked/quota de projeto etc — retry so piora)."""
    s = f"{type(e).__name__} {e}".lower()
    return any(k in s for k in _IA_ERR_TRANSITORIO)


def process_and_respond(user_id):
    """Serializa o turno deste cliente e drena o buffer ANTES de executá-lo.

    Ordem: adquire turn_lock -> pop do buffer (sob buffer_lock) -> executa.
    Bloquear ANTES de drenar é de propósito: o turno B fica esperando com o buffer
    ainda cheio, então a msg3 que chegar entra no MESMO buffer e B a drena junto.
    Drenar antes de bloquear geraria duas respostas separadas.
    """
    turn_lock = _get_turn_lock(user_id)
    buf_lock = _get_user_lock(user_id)      # resolve AMBOS antes de adquirir qualquer um
    t0 = time.perf_counter()
    with turn_lock:
        espera_ms = int((time.perf_counter() - t0) * 1000)
        with buf_lock:
            buffer = message_buffers.pop(user_id, None)
        if buffer is None:                  # 'is None': dict drenado e sempre truthy
            print(f"[TURNO] {user_id}: buffer vazio (ja coalescido por outro turno)")
            return
        tmr = buffer.get("timer")
        if tmr is not None:
            try:
                tmr.cancel()
            except Exception:
                pass
        texto_completo = (buffer.get("text") or "")
        if not texto_completo.strip():
            print(f"[TURNO] {user_id}: texto vazio — ignorando")
            return
        print(f"[TURNO] {user_id}: espera_lock={espera_ms}ms | chars={len(texto_completo)}")
        print(f"DEBUG INPUT: '{texto_completo}'")
        # Contexto do turno usado pelas tools (defesa B4 em consultar_estoque).
        # É por THREAD: dois clientes simultâneos não enxergam o texto um do outro.
        _set_turn_ctx(user_id, texto_completo)
        fotos_pendentes = []                # F3 usa; aqui fica vazio e inerte
        turno_ok = False
        try:
            _executar_turno(user_id, texto_completo, fotos_pendentes)
            turno_ok = True
        except Exception as e:
            # save_message roda DENTRO de _executar_turno; sem este try uma falha de
            # rede ali descartaria a mensagem do cliente em silêncio (o buffer já foi
            # drenado, então não há mais o "retry acidental" que o vazamento dava).
            traceback.print_exc()
            try:
                enviar_mensagem_whatsapp(user_id, "Ops, tive um probleminha tecnico aqui 😅 Pode reenviar sua ultima mensagem, por favor?")
            except Exception:
                pass
            try:
                log_turn(user_id, texto_completo, [], "", 0, "", "error", True, error=str(e))
            except Exception:
                pass
        finally:
            _clear_turn_ctx()
        return turno_ok


def _executar_turno(user_id, texto_completo, fotos_pendentes=None):
    """Corpo do turno: config -> Gemini (tools) -> render -> log.

    NÃO mexe em buffer nem em lock; assume turno já serializado por
    `process_and_respond` e contexto de thread já setado.
    """
    # NAO mover este save para depois do get_history: durante o silencio pos-handoff
    # a atendente humana le a mensagem do cliente em chat_history, e
    # _montar_mensagem_operador tambem a le no MEIO do turno.
    msg_id = save_message(user_id, "user", texto_completo)

    try:
        # --- BUSCA CONFIGURAÇÃO NO BANCO ---
        # A cada mensagem, verificamos se o bot está ativo e qual o prompt atual.
        try:
             config = supabase.table("bot_settings").select("*").eq("id", 1).single().execute()
        except Exception as e_config:
             print(f"⚠️ Erro ao buscar configs: {e_config}")
             config = None
        
        # Se não conseguir ler config ou is_active for False, ignora/aborta.
        # Ajuste: Se config for None (tabela não existe ou erro), assumimos INATIVO por segurança ou defina um fallback?
        # O prompt pede: "Se is_active for falso, o backend deve ignorar novas mensagens."
        
        if not config or not config.data:
            print("⚠️ Configuração não encontrada. Bot inativo.")
            return

        settings = config.data
        if not settings.get('is_active'):
             print(f"⏹️ Bot desligado no painel para usuário. Ignorando mensagem.")
             return

        # Pós-handoff: humano assumiu, bot fica em silêncio por HANDOFF_SILENCIO_MIN.
        # Mensagem do cliente já foi salva em chat_history (acima) — atendente vê.
        if _em_silencio_pos_handoff(user_id):
            print(f"⏸️ Handoff ativo para {user_id} — bot em silêncio, ignorando msg.")
            return

        system_instruction_dinamica = settings.get('system_prompt', '')
        
        if not system_instruction_dinamica:
             # Fallback caso o prompt esteja vazio no banco (opcional, mas bom pra evitar crash)
             system_instruction_dinamica = "Você é um assistente útil."
        
        # Instancia o modelo com a instrução ATUALIZADA
        transferir_para_atendente = criar_tool_transferir(user_id)

        # Tenta com response_schema; se a versão do SDK / modelo recusar
        # generation_config + tools, recria sem schema (cai no fallback regex).
        modelo_args = dict(
            model_name='gemini-3-flash-preview',
            tools=[
                consultar_estoque_supabase,
                consultar_produto_por_id,
                calcular_total,
                verificar_promocao_hoje,
                transferir_para_atendente,
                calcular_frete_estimado,
            ],
            system_instruction=system_instruction_dinamica,
            safety_settings=SAFETY_SETTINGS,
        )
        try:
            model = genai.GenerativeModel(generation_config=GENERATION_CONFIG, **modelo_args)
            json_mode_enabled = True
        except Exception as e_schema:
            print(f"⚠️ response_schema rejeitado pelo SDK ({e_schema}); modo livre.")
            model = genai.GenerativeModel(**modelo_args)
            json_mode_enabled = False

        # A mensagem atual vai ao modelo UMA vez, por chat.send_message() la embaixo.
        # Antes ela vinha tambem no history (o save acontece acima) — o modelo via a
        # mesma frase duas vezes, em dois turnos 'user' adjacentes.
        history = get_history(user_id, excluir_id=msg_id)
        if (msg_id is None and history and history[-1]["role"] == "user"
                and history[-1]["parts"] == [texto_completo]):
            print("[HIST] insert sem id; removendo a linha atual por texto")
            history = history[:-1]
        history = _normalizar_history_para_gemini(history)
        t_inicio = time.perf_counter()
        # Gemini as vezes trava/estoura o deadline (504), sobretudo em turns com cadeia
        # de tools (ex.: resposta-a-card -> consultar_estoque + calcular_total). Sem retry
        # o cliente caia no fallback "reenvie" e a Luna parecia muda. Retry com o chat
        # reconstruido a cada tentativa (history vem do banco e e estavel).
        response = None
        _MAX_TENTATIVAS_IA = 3
        for _tentativa in range(1, _MAX_TENTATIVAS_IA + 1):
            chat = model.start_chat(history=history, enable_automatic_function_calling=True)
            try:
                response = chat.send_message(texto_completo, request_options={"timeout": 45})
                break
            except Exception as e_ia:
                if _tentativa < _MAX_TENTATIVAS_IA and _erro_ia_transitorio(e_ia):
                    print(f"⚠️ IA transitorio (tentativa {_tentativa}/{_MAX_TENTATIVAS_IA}): "
                          f"{type(e_ia).__name__}: {str(e_ia)[:120]} — retry")
                    time.sleep(2 * _tentativa)
                    continue
                raise
        latencia_ms = int((time.perf_counter() - t_inicio) * 1000)
        resposta_texto = response.text
        print(f"DEBUG OUTPUT ({latencia_ms}ms): '{resposta_texto[:200]}'")

        save_message(user_id, "model", resposta_texto)

        # Cache canônico de produtos retornados pelas tools no turno
        chat_history_obj = getattr(chat, 'history', None)
        cache_produtos = extrair_produtos_de_tool_results(chat_history_obj)
        tool_calls_serializados = serializar_tool_calls(chat_history_obj)

        # Captura tokens Gemini (usage_metadata) para tracking de custo
        tokens_in_count = None
        tokens_out_count = None
        try:
            usage = getattr(response, 'usage_metadata', None)
            if usage:
                tokens_in_count = getattr(usage, 'prompt_token_count', None)
                tokens_out_count = getattr(usage, 'candidates_token_count', None)
        except Exception:
            pass

        # Caminho preferencial: JSON estruturado
        resposta_limpa, ids_recomendados, json_ok, modo_preco = parsear_resposta_json(resposta_texto)

        fallback_usado = False
        if json_ok:
            modo_efetivo = _modo_preco_efetivo(user_id, modo_preco, texto_completo)
            print(f"✅ JSON parse ok | ids={ids_recomendados} | modo_preco={modo_preco}->{modo_efetivo} | cache_size={len(cache_produtos)}")
            renderizar_mensagem_estruturada(user_id, resposta_limpa, ids_recomendados, cache_produtos, modo_efetivo)
        else:
            fallback_usado = True
            print(f"⚠️ JSON parse falhou (json_mode={json_mode_enabled}); fallback regex.")
            renderizar_mensagem_regex_fallback(user_id, resposta_texto)

        log_turn(
            user_id=user_id,
            user_input=texto_completo,
            tool_calls=tool_calls_serializados,
            final_output=resposta_texto,
            latency_ms=latencia_ms,
            model_name='gemini-3-flash-preview',
            output_format=("json" if json_ok else "text"),
            fallback_used=fallback_usado,
            tokens_in=tokens_in_count,
            tokens_out=tokens_out_count,
        )

    except Exception as e:
        import traceback
        err_str = f"{e}\n{traceback.format_exc()}"
        print(f"Erro IA: {err_str}")
        # Degradacao graciosa: NUNCA deixar o cliente sem resposta (nem em bloqueio/timeout).
        _low = str(e).lower()
        if any(k in _low for k in ("block", "prohibited", "safety", "finish_reason", "blocked")):
            _fallback = "Desculpa, nao consegui processar essa mensagem agora 😅 Pode reformular ou me dizer de outro jeito? Se preferir, ja chamo uma atendente pra te ajudar 💕"
        else:
            _fallback = "Ops, tive um probleminha tecnico aqui 😅 Pode reenviar sua ultima mensagem, por favor?"
        try:
            enviar_mensagem_whatsapp(user_id, _fallback)
        except Exception:
            pass
        # Fecha o par user/model: sem isto todo turno com erro deixava uma linha
        # 'user' orfa e, como o fallback pede reenvio, o cliente reenviava e nascia
        # o par 'user'/'user' identico que envenena o contexto do turno seguinte.
        try:
            save_message(user_id, "model", _fallback)
        except Exception:
            pass
        try:
            log_turn(
                user_id=user_id,
                user_input=texto_completo,
                tool_calls=[],
                final_output=_fallback,
                latency_ms=0,
                model_name='gemini-3-flash-preview',
                output_format="error",
                fallback_used=False,
                error=str(e),
            )
        except Exception:
            pass


def _cloud_send(payload):
    """POST na WhatsApp Cloud API (Graph). Retorna o wamid (str) ou None. Levanta em erro HTTP."""
    if not _GRAPH_MSG_URL or not WHATSAPP_TOKEN:
        print("[cloud] WHATSAPP_PHONE_NUMBER_ID/WHATSAPP_TOKEN ausentes; envio ignorado.")
        return None
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(_GRAPH_MSG_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    try:
        return (resp.json().get("messages") or [{}])[0].get("id")
    except Exception:
        return None


# ---- Midia RECEBIDA do cliente (audio/imagem): download + Gemini (multimodal) ----
_CLOUD_MEDIA_MAX_BYTES = 16 * 1024 * 1024   # teto de midia da Cloud API
MODEL_MULTIMODAL = "gemini-3-flash-preview"  # mesmo modelo do pipeline de venda


def _cloud_baixar_midia(media_id):
    """media_id -> (bytes, mime) ou (None, None) em qualquer falha. DOIS GETs, ambos
    com Authorization: Bearer WHATSAPP_TOKEN:
      1) GET graph.facebook.com/{GRAPH_VERSION}/{media_id} -> {url, mime_type, file_size}
      2) GET nessa url (lookaside.fbsbx.com, efemera ~5min) -> bytes."""
    if not media_id or not WHATSAPP_TOKEN:
        print("[cloud] baixar_midia: media_id/WHATSAPP_TOKEN ausentes")
        return None, None
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
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
        return (bytes(buf), (mime or None)) if buf else (None, None)
    except Exception as e:
        print(f"[cloud] baixar_midia falhou | id={media_id}: {e}")
        return None, None


def transcrever_audio(audio_bytes, mime):
    """Transcreve audio recebido do cliente (inline, sem File API). str ou None."""
    if not audio_bytes:
        return None
    try:
        model = genai.GenerativeModel(MODEL_MULTIMODAL, safety_settings=SAFETY_SETTINGS)
        prompt = ("Transcreva o audio a seguir em portugues do Brasil. Responda APENAS com "
                  "a transcricao literal do que foi dito, sem comentarios, sem aspas, sem rotulos.")
        resp = model.generate_content(
            [prompt, {"mime_type": mime or "audio/ogg", "data": audio_bytes}],
            request_options={"timeout": 45},
        )
        return ((getattr(resp, "text", "") or "").strip()) or None
    except Exception as e:
        print(f"[gemini] transcrever_audio falhou: {e}")
        return None


def descrever_imagem(img_bytes, mime, caption=None):
    """Descreve imagem recebida do cliente (peca) p/ busca no catalogo. str ou None."""
    if not img_bytes:
        return None
    try:
        model = genai.GenerativeModel(MODEL_MULTIMODAL, safety_settings=SAFETY_SETTINGS)
        prompt = ("Voce ajuda uma loja de lingerie. Descreva de forma objetiva e curta a peca na "
                  "imagem para busca no catalogo: tipo (sutia/calcinha/conjunto/camisola etc), cor, "
                  "estampa e detalhes (renda, boju, fio), e QUALQUER codigo/numero/texto visivel na "
                  "foto ou etiqueta. Se nao for uma peca de lingerie, diga o que aparenta ser em 1 "
                  "frase. Nao invente codigo.")
        if caption:
            prompt += f' Legenda enviada pelo cliente: "{caption}".'
        resp = model.generate_content(
            [prompt, {"mime_type": mime or "image/jpeg", "data": img_bytes}],
            request_options={"timeout": 45},
        )
        return ((getattr(resp, "text", "") or "").strip()) or None
    except Exception as e:
        print(f"[gemini] descrever_imagem falhou: {e}")
        return None


def enviar_midia_whatsapp(numero, url_midia, legenda):
    """Envia imagem com legenda. Retorna wamid (cloud) ou None. Backend por WHATSAPP_PROVIDER."""
    if WHATSAPP_PROVIDER == "cloud":
        try:
            print(f"📸 [cloud] Enviando mídia para {numero}: {url_midia}")
            return _cloud_send({
                "messaging_product": "whatsapp", "to": numero, "type": "image",
                "image": {"link": url_midia, "caption": legenda},
            })
        except Exception as e:
            print(f"Erro Cloud API (mídia): {e}")
            return None

    # --- UAZAPI (default) ---
    # Deriva a URL de mídia baseada na URL de texto configurada no ENV
    # Ex: .../send/text -> .../send/media
    if UAZAPI_URL and "send/text" in UAZAPI_URL:
        url = UAZAPI_URL.replace("send/text", "send/media")
    else:
        # Fallback ou se a URL for diferente
        url = "https://vennx.uazapi.com/send/media"

    headers = {"token": UAZAPI_TOKEN, "Content-Type": "application/json"}
    payload = {
        "number": numero,
        "type": "image",  # Assumindo imagem por enquanto
        "file": url_midia,
        "docName": "foto_produto.jpg",
        "text": legenda,  # Legenda vai no campo text conforme documentação
    }
    try:
        print(f"📸 Enviando Mídia para {numero}: {url_midia}")
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"Status Mídia: {response.status_code} | {response.text}")
    except Exception as e:
        print(f"Erro de conexão Uazapi Media: {e}")
    return None


def enviar_mensagem_whatsapp(numero, texto):
    """Envia mensagem de texto. Retorna wamid (cloud) ou None. Backend por WHATSAPP_PROVIDER."""
    if WHATSAPP_PROVIDER == "cloud":
        try:
            return _cloud_send({
                "messaging_product": "whatsapp", "to": numero, "type": "text",
                "text": {"body": texto, "preview_url": False},
            })
        except Exception as e:
            print(f"Erro Cloud API (texto): {e}")
            return None

    # --- UAZAPI (default) ---
    headers = {"token": UAZAPI_TOKEN, "Content-Type": "application/json"}
    payload = {"number": numero, "text": texto}
    try:
        requests.post(UAZAPI_URL, json=payload, headers=headers, timeout=30)
    except Exception as e:
        print(f"Erro de conexão Uazapi: {e}")
    return None

def _extract_message_text(msg_data):
    """Extrai texto do payload do webhook, com fallback para mensagens citadas (botão Responder).

    Quando o cliente usa "Responder" no WhatsApp, o messageType muda para
    extendedTextMessage e o texto pode chegar em campos diferentes ou aninhado
    dentro de um dict. Cobre as variantes mais comuns da UAZAPI / Baileys.
    """
    candidatos = []
    candidatos.append(msg_data.get('content'))
    candidatos.append(msg_data.get('text'))
    candidatos.append(msg_data.get('conversation'))
    candidatos.append(msg_data.get('body'))

    ext = msg_data.get('extendedTextMessage')
    if isinstance(ext, dict):
        candidatos.append(ext.get('text'))

    msg_inner = msg_data.get('message')
    if isinstance(msg_inner, dict):
        candidatos.append(msg_inner.get('conversation'))
        ext_inner = msg_inner.get('extendedTextMessage')
        if isinstance(ext_inner, dict):
            candidatos.append(ext_inner.get('text'))

    for c in candidatos:
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, dict):
            nested = c.get('text') or c.get('conversation') or c.get('body')
            if isinstance(nested, str) and nested.strip():
                return nested
    return None


def _extract_quoted_content(msg_data):
    """Extrai o conteudo da mensagem citada (botao Responder do WhatsApp).

    Tenta varios paths conhecidos da UAZAPI/Baileys. Retorna string com o conteudo
    citado (texto, legenda de imagem, ou nome do arquivo) ou None se nao houver.
    """
    if not isinstance(msg_data, dict):
        return None

    # Caminhos onde a quotedMessage pode estar aninhada
    quoted_paths = [
        msg_data.get('quotedMessage'),
        msg_data.get('quoted'),
        (msg_data.get('contextInfo') or {}).get('quotedMessage') if isinstance(msg_data.get('contextInfo'), dict) else None,
    ]

    # UAZAPI variante: content eh dict com text + contextInfo.quotedMessage
    content_dict = msg_data.get('content')
    if isinstance(content_dict, dict):
        quoted_paths.append(content_dict.get('quotedMessage'))
        ctx_c = content_dict.get('contextInfo')
        if isinstance(ctx_c, dict):
            quoted_paths.append(ctx_c.get('quotedMessage'))

    ext = msg_data.get('extendedTextMessage')
    if isinstance(ext, dict):
        ctx = ext.get('contextInfo')
        if isinstance(ctx, dict):
            quoted_paths.append(ctx.get('quotedMessage'))
    msg_inner = msg_data.get('message')
    if isinstance(msg_inner, dict):
        ext_inner = msg_inner.get('extendedTextMessage')
        if isinstance(ext_inner, dict):
            ctx_inner = ext_inner.get('contextInfo')
            if isinstance(ctx_inner, dict):
                quoted_paths.append(ctx_inner.get('quotedMessage'))

    # Tambem aceita campos flat que algumas integracoes mandam
    flat_candidates = [
        msg_data.get('quotedText'),
        msg_data.get('quotedContent'),
        msg_data.get('quotedBody'),
        msg_data.get('replyTo'),
    ]

    for q in quoted_paths:
        if not isinstance(q, dict):
            continue
        # Texto plano citado
        for key in ('conversation', 'text', 'body'):
            v = q.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # Texto dentro de extendedTextMessage citado
        ext_q = q.get('extendedTextMessage')
        if isinstance(ext_q, dict):
            v = ext_q.get('text')
            if isinstance(v, str) and v.strip():
                return v.strip()
        # Legenda de imagem citada (e o caso dos cards de produto)
        img = q.get('imageMessage')
        if isinstance(img, dict):
            cap = img.get('caption')
            if isinstance(cap, str) and cap.strip():
                return cap.strip()
        # Documento/audio/video citado: usar caption ou fileName
        for media_key in ('videoMessage', 'documentMessage', 'audioMessage'):
            m = q.get(media_key)
            if isinstance(m, dict):
                cap = m.get('caption') or m.get('fileName') or m.get('title')
                if isinstance(cap, str) and cap.strip():
                    return cap.strip()

    for c in flat_candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, dict):
            for key in ('text', 'caption', 'conversation', 'body'):
                v = c.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


# ================= TRANSPORTE CLOUD API — HELPERS =================
# Dedup de wamid recebido (Meta reentrega quando nao recebe 200 rapido). Best-effort
# por-processo — em multiplas replicas, migrar p/ store compartilhado (MIGRATION_CLOUD_API.md).
_cloud_seen_wamids = collections.deque()
_cloud_seen_set = set()
_CLOUD_DEDUP_MAX = 3000


def _cloud_ja_processado(wamid):
    if not wamid:
        return False
    if wamid in _cloud_seen_set:
        return True
    _cloud_seen_set.add(wamid)
    _cloud_seen_wamids.append(wamid)
    while len(_cloud_seen_wamids) > _CLOUD_DEDUP_MAX:
        _cloud_seen_set.discard(_cloud_seen_wamids.popleft())
    return False


def _canonical_user_id(num):
    """Chave estavel de conversa. Trata o 9o digito de celular BR: a Cloud API pode
    entregar 55+DDD+8digitos (sem o 9); reinsere o 9 quando o local comeca com 6-9
    (prefixos de celular), preservando compatibilidade com o historico gravado no
    formato da UAZAPI. Landline (comeca com 2-5) fica intacto."""
    d = re.sub(r"\D", "", str(num or ""))
    if d.startswith("55") and len(d) == 12 and d[4] in "6789":
        d = d[:4] + "9" + d[4:]
    return d


def registrar_card_enviado(wamid, user_id, id_produto, legenda=None):
    """Persiste wamid->id_produto (tabela card_envios) p/ o reply-to-card no cloud.
    Degrada em silencio se a tabela nao existir (DDL em MIGRATION_CLOUD_API.md)."""
    try:
        supabase.table("card_envios").insert({
            "wamid": wamid,
            "user_id": user_id,
            "id_produto": int(id_produto),
            "legenda": legenda,
        }).execute()
    except Exception as e:
        print(f"[card_envios] falha ao registrar {wamid}: {e}")


def _lookup_card(wamid):
    """id_produto do card cujo wamid o cliente citou (context.id), ou None."""
    if not wamid:
        return None
    try:
        r = (supabase.table("card_envios").select("id_produto")
             .eq("wamid", wamid).limit(1).execute())
        rows = r.data or []
        return int(rows[0]["id_produto"]) if rows else None
    except Exception as e:
        print(f"[card_envios] lookup {wamid}: {e}")
        return None


def _enqueue_user_message(user_id, raw_text):
    """Rate-limit + buffer/debounce (10s) + agenda process_and_respond.
    Compartilhado pelos dois providers (UAZAPI e Cloud). Retorna False se rate-limited."""
    if not _check_rate_limit(user_id):
        print(f"[RATE_LIMIT] user={user_id} excedeu {RATE_LIMIT_MAX_MSGS} msgs/{RATE_LIMIT_WINDOW_SEC}s — descartando")
        return False
    user_lock = _get_user_lock(user_id)
    with user_lock:
        if user_id not in message_buffers:
            message_buffers[user_id] = {"text": raw_text, "timer": None}
        else:
            message_buffers[user_id]["text"] += f" {raw_text}"
            existing_timer = message_buffers[user_id].get("timer")
            if existing_timer:
                existing_timer.cancel()
        t = threading.Timer(10.0, process_and_respond, args=[user_id])
        # daemon: um Timer de 10s pendente penduraria qualquer processo de teste
        # que chame _enqueue_user_message (T0/T1) ate o timer disparar.
        t.daemon = True
        message_buffers[user_id]["timer"] = t
        t.start()
    return True


def _processar_midia_async(numero, tipo, media_id, caption, ctx_id):
    """Baixa a midia recebida, transcreve (audio) ou descreve (imagem) via Gemini e injeta
    como texto no pipeline (mesmo molde do reply-to-card). Roda em THREAD para o webhook
    responder 200 rapido (evita reentrega da Meta). Falha -> pede texto ao cliente."""
    try:
        b, mime = _cloud_baixar_midia(media_id)
        if tipo == "audio":
            txt = transcrever_audio(b, mime) if b else None
            if not txt:
                enviar_mensagem_whatsapp(numero, "Não consegui ouvir seu áudio 😅 me escreve o que você precisa?")
                return
            raw_text = f"[transcrição de áudio do cliente:] {txt}"
        else:  # image
            desc = descrever_imagem(b, mime, caption) if b else None
            if not desc:
                enviar_mensagem_whatsapp(numero, "Recebi sua foto mas não consegui abrir 😅 me manda o código da peça ou descreve pra mim?")
                return
            raw_text = f"[o cliente enviou uma foto que parece: {desc}]"
            if caption:
                raw_text += f' Legenda do cliente: "{caption}"'
        if ctx_id:  # reply-to-card: cliente respondeu um card com audio/foto
            pid = _lookup_card(ctx_id)
            if pid:
                raw_text = (f"[o cliente respondeu ao card do produto id_produto={pid}. "
                            f"Use EXATAMENTE id_produto={pid} para este item em calcular_total / "
                            f"produtos_recomendados / resumo; nao re-derive o id pelo nome.] {raw_text}")
        _enqueue_user_message(numero, raw_text)
    except Exception as e:
        print(f"[cloud] _processar_midia_async falhou | type={tipo} from={numero}: {e}")


def _ingest_cloud_message(msg, value):
    """Parseia UMA mensagem do webhook da Cloud API e enfileira p/ processamento."""
    wamid = msg.get("id")
    if _cloud_ja_processado(wamid):
        return
    numero = _canonical_user_id(msg.get("from") or "")
    if not numero:
        return
    tipo = msg.get("type")
    if tipo in ("audio", "image"):
        # midia: baixa + Gemini + injeta em THREAD; webhook responde 200 na hora
        media = msg.get(tipo) or {}
        caption = (media.get("caption") or "").strip() if tipo == "image" else None
        ctx_id = (msg.get("context") or {}).get("id")
        threading.Thread(
            target=_processar_midia_async,
            args=(numero, tipo, media.get("id"), caption, ctx_id),
            daemon=True,
        ).start()
        return
    if tipo == "text":
        raw_text = (msg.get("text") or {}).get("body", "")
    elif tipo == "interactive":
        inter = msg.get("interactive") or {}
        br = inter.get("button_reply") or inter.get("list_reply") or {}
        raw_text = br.get("title") or br.get("id") or ""
    elif tipo == "button":
        raw_text = (msg.get("button") or {}).get("text", "")
    else:
        raw_text = ""  # midia/localizacao/etc — sem texto util por enquanto
    if not raw_text:
        print(f"[cloud] mensagem sem texto util | type={tipo} | from={numero}")
        return

    # reply-to-card: a Cloud API so devolve o wamid citado (context.id), nao a legenda.
    ctx_id = (msg.get("context") or {}).get("id")
    if ctx_id:
        pid = _lookup_card(ctx_id)
        if pid:
            raw_text = (f"[o cliente respondeu ao card do produto id_produto={pid}. "
                        f"Use EXATAMENTE id_produto={pid} para este item em calcular_total / "
                        f"produtos_recomendados / resumo; nao re-derive o id pelo nome.] {raw_text}")

    _enqueue_user_message(numero, raw_text)


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Handshake de verificacao do webhook da Cloud API (Meta faz um GET nesta URL)."""
    if (WHATSAPP_VERIFY_TOKEN
            and request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.route('/webhook', methods=['POST'])
@app.route('/webhook/<evento>/<tipo>', methods=['POST'])
def webhook(evento=None, tipo=None):
    # --- WhatsApp Cloud API (oficial) ---
    if WHATSAPP_PROVIDER == "cloud":
        raw = request.get_data()
        if WHATSAPP_APP_SECRET:
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                print("[cloud] assinatura X-Hub-Signature-256 invalida — descartando")
                return jsonify({"status": "bad_signature"}), 403
        data = request.get_json(silent=True) or {}
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue  # ignora template/quality/account updates
                value = change.get("value", {})
                for m in value.get("messages", []):  # ignora value["statuses"] (recibos)
                    _ingest_cloud_message(m, value)
        return jsonify({"status": "ok"}), 200

    # --- UAZAPI (default) ---
    data = request.json
    msg_data = data.get('message', {})
    if msg_data.get('fromMe'): return jsonify({"status": "ignored"}), 200

    chat_id = msg_data.get('chatid')
    raw_text = _extract_message_text(msg_data)
    quoted = _extract_quoted_content(msg_data)

    # Log temporario do payload quando vier mensagem citada (botao Responder).
    # Remover apos confirmar o campo correto da UAZAPI.
    msg_type = msg_data.get('messageType') or msg_data.get('type') or ''
    is_reply = quoted is not None or 'extend' in str(msg_type).lower() or 'quot' in str(msg_type).lower() or msg_data.get('quotedMessage') is not None
    if is_reply:
        try:
            payload_str = json.dumps(msg_data, ensure_ascii=False, default=str)[:2000]
            print(f"[WEBHOOK_REPLY] type={msg_type} | quoted_extraido={quoted!r} | payload={payload_str}")
        except Exception as e:
            print(f"[WEBHOOK_REPLY] falha ao serializar payload: {e} | keys={list(msg_data.keys())}")

    # Se houver quoted, anexa ao texto para a LLM ter contexto do que o cliente citou.
    if isinstance(raw_text, str) and quoted:
        _m_cod = re.search(r'c[óo]d:\s*(\d+)', quoted, re.IGNORECASE)
        if _m_cod:
            _pid = _m_cod.group(1)
            raw_text = (f"[o cliente respondeu ao card do produto id_produto={_pid} "
                        f"(citacao: \"{quoted}\"). Use EXATAMENTE id_produto={_pid} para este item em "
                        f"calcular_total / produtos_recomendados / resumo; nao re-derive o id pelo nome.] {raw_text}")
        else:
            raw_text = f"[respondendo a mensagem citada: \"{quoted}\"] {raw_text}"

    if not chat_id or not isinstance(raw_text, str):
        if chat_id:
            print(f"[WEBHOOK] texto nao extraido | chat={chat_id} | type={msg_type} | keys={list(msg_data.keys())}")
        return jsonify({"status": "buffering"}), 200

    user_id = chat_id.split('@')[0]

    if not _enqueue_user_message(user_id, raw_text):
        return jsonify({"status": "rate_limited"}), 200
    return jsonify({"status": "buffering"}), 200

# ============================================================
# Health-check + watchdog do sync (alerta se o sync travar)
# ============================================================
PROCESS_START_AT = datetime.now(timezone.utc)
SYNC_STALE_ALERT_MIN = int(os.getenv("SYNC_STALE_ALERT_MIN", "30"))
ALERT_WHATSAPP = os.getenv("ALERT_WHATSAPP", "")   # numero sem '+', ex: 5514997767200
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")
_alert_state = {"alerting": False, "last_alert_at": None}


def _sync_age_min():
    """Retorna (idade_min, last_run_dt|None, tolerancia_min) usando o heartbeat do sync_erp."""
    now = datetime.now(timezone.utc)
    last = sync_erp.LAST_RUN_AT
    if last is None:
        # Ainda nao rodou desde o boot: tolera boot + 1 ciclo antes de considerar parado.
        return (now - PROCESS_START_AT).total_seconds() / 60.0, None, SYNC_STALE_ALERT_MIN + 15
    return (now - last).total_seconds() / 60.0, last, SYNC_STALE_ALERT_MIN


@app.route('/health', methods=['GET'])
def health():
    age, last, tol = _sync_age_min()
    healthy = age <= tol
    body = {
        "status": "ok" if healthy else "stale",
        "sync_last_run_utc": last.isoformat() if last else None,
        "sync_age_min": round(age, 1),
        "sync_last_ok": sync_erp.LAST_RUN_OK,
        "sync_last_info": sync_erp.LAST_RUN_INFO,
        "threshold_min": tol,
        # ALARME: >0 persistente em repouso = buffer orfao, o vazamento voltou.
        "buffers_pendentes": len(message_buffers),
        # CONTADOR cumulativo de clientes distintos atendidos desde o boot — so
        # cresce, NAO e alarme (um lock por user_id, criado on-demand).
        "turnos_conhecidos_desde_boot": len(_user_turn_locks),
    }
    return jsonify(body), (200 if healthy else 503)


def _enviar_email_alerta(assunto, corpo):
    host = os.getenv("SMTP_HOST"); user = os.getenv("SMTP_USER"); pwd = os.getenv("SMTP_PASS")
    if not (host and user and pwd and ALERT_EMAIL):
        return False  # e-mail nao configurado (defina SMTP_HOST/USER/PASS p/ ativar)
    try:
        import smtplib
        from email.mime.text import MIMEText
        m = MIMEText(corpo, "plain", "utf-8")
        m["Subject"] = assunto; m["From"] = user; m["To"] = ALERT_EMAIL
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls(); s.login(user, pwd); s.send_message(m)
        print(f"[ALERTA] email enviado para {ALERT_EMAIL}")
        return True
    except Exception as e:
        print(f"[ALERTA] email falhou: {e}")
        return False


def _disparar_alerta(assunto, corpo):
    if ALERT_WHATSAPP:
        try:
            enviar_mensagem_whatsapp(ALERT_WHATSAPP, f"*{assunto}*\n\n{corpo}")
            print(f"[ALERTA] whatsapp enviado para {ALERT_WHATSAPP}")
        except Exception as e:
            print(f"[ALERTA] whatsapp falhou: {e}")
    _enviar_email_alerta(assunto, corpo)


def _verificar_sync_e_alertar():
    """Roda periodicamente: se o sync nao roda ha mais que o limite, alerta (com cooldown)."""
    try:
        now = datetime.now(timezone.utc)
        age, last, tol = _sync_age_min()
        if age > tol:
            la = _alert_state["last_alert_at"]
            reenvio = la is None or (now - la).total_seconds() > 3 * 3600
            if not _alert_state["alerting"] or reenvio:
                corpo = (
                    "O sync de estoque do bot Sangali parece parado.\n"
                    f"Ultima execucao: {last.isoformat() if last else 'nenhuma desde o boot'}\n"
                    f"Ha ~{int(age)} min (limite {int(tol)} min).\n"
                    "Verifique o servico no Railway."
                )
                _disparar_alerta("Sync de estoque parado", corpo)
                _alert_state["alerting"] = True
                _alert_state["last_alert_at"] = now
                print(f"[WATCHDOG] alerta de sync parado enviado (age={int(age)}min)")
        else:
            if _alert_state["alerting"]:
                _disparar_alerta("Sync de estoque normalizado",
                                 f"O sync voltou a rodar (ultima execucao ha ~{int(age)} min).")
                print("[WATCHDOG] sync normalizado")
            _alert_state["alerting"] = False
    except Exception as e:
        print(f"[WATCHDOG] erro: {e}")


if __name__ == '__main__':
    # Inicia os schedulers de sincronização
    scheduler = BackgroundScheduler()
    # Sync ERP in-process (fallback). Ideal em producao: rodar via cron dedicado
    # do Railway (python sync_erp.py), desacoplado do web — nao trava o webhook e
    # nao depende deste processo. Desligue este aqui com ENABLE_INPROCESS_SYNC=0
    # quando o cron dedicado estiver ativo (evita sync duplicado).
    if os.getenv("ENABLE_INPROCESS_SYNC", "1") == "1":
        _sync_min = int(os.getenv("SYNC_INTERVAL_MIN", "10"))
        scheduler.add_job(sync_otimizado, 'interval', minutes=_sync_min,
                          misfire_grace_time=600, coalesce=True, max_instances=1)
        print(f"Scheduler ERP in-process ativo: cada {_sync_min}min")
    scheduler.add_job(sync_images, 'cron', hour=9, minute=0, misfire_grace_time=3600)
    scheduler.add_job(_job_purge_bot_turns, 'cron', hour=7, minute=0, misfire_grace_time=3600)
    scheduler.add_job(_verificar_sync_e_alertar, 'interval', minutes=15,
                      misfire_grace_time=300, coalesce=True, max_instances=1)
    scheduler.start()
    print("Schedulers iniciados: Imagens diario 6h BRT | Purge bot_turns diario 4h BRT")

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)