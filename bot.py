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

# ---------------- FOTOS SOB DEMANDA (F3) ----------------
# Kill switch: com FOTOS_SOB_DEMANDA=0 a tool sai da function declaration e o
# sender vira no-op — rollback sem redeploy. `n_fotos`/ordem determinística
# continuam valendo (são inofensivos e corrigem o card).
FOTOS_SOB_DEMANDA = (os.getenv("FOTOS_SOB_DEMANDA", "1") != "0")
# Teto de fotos extras por PEDIDO (= por produto, = por turno: um produto por turno).
# Default 4 por dado: cobre INTEGRALMENTE 191 dos 208 produtos com 2+ fotos e
# estoque; os 17 restantes (6-10 fotos) paginam pedindo de novo. Trocar o teto é
# variável de ambiente, NÃO código — a decisão comercial (§10.3 item 7) segue aberta.
FOTOS_MAX_POR_PEDIDO = max(1, int(os.getenv("FOTOS_MAX_POR_PEDIDO", "4")))
# Um produto por turno: se PRODUTO e TURNO divergissem, a tool prometeria 4+4 e o
# sender entregaria 4+2 — a tool tem de prometer exatamente o que o sender manda.
FOTOS_MAX_POR_TURNO = FOTOS_MAX_POR_PEDIDO
FOTOS_SLEEP_SEG = float(os.getenv("FOTOS_SLEEP_SEG", "0.8"))
FOTOS_TTL_SEG = float(os.getenv("FOTOS_TTL_SEG", str(6 * 3600)))
FOTOS_MAX_USERS_REGISTRY = 500
# Registro de fotos JÁ ENVIADAS (memória do PROCESSO — exige replicas=1, Gate 0.1).
# {user_id: {pid(str): {url: ts_envio}}}, com LRU por user_id e TTL por URL.
# É a guarda de idempotência de último nível: mesmo que o sender rode duas vezes,
# a URL já marcada não sai de novo. Restart zera (pior caso: 1 foto repetida).
_fotos_vistas = collections.OrderedDict()
_fotos_vistas_lock = threading.Lock()

# ---------------- RANKING COMERCIAL (F5) ----------------
# Pool de candidatos que a RPC ordena por similaridade ANTES de deduplicar por
# produto. 60 e medido: com 10 a busca devolvia 3-8 produtos DISTINTOS (as outras
# linhas eram o mesmo produto em outro tamanho); com 60 o pool tem 18-36 distintos
# e sempre da para preencher o corte de 8.
POOL_CANDIDATOS = int(os.getenv("POOL_CANDIDATOS", "60"))
# Corte final: 8 produtos DISTINTOS. Nao alonga o turno — renderizar_mensagem_
# estruturada tem cap de 5 cards e o prompt limita a 3 recomendacoes.
LIMITE_PRODUTOS = int(os.getenv("LIMITE_PRODUTOS", "8"))
# Quantos lugares do topo pertencem a similaridade pura (tier 0). A categoria que
# o cliente pediu nunca perde o topo para um campeao de venda.
ANCORA_SEMANTICA = int(os.getenv("ANCORA_SEMANTICA", "2"))
# Janela absoluta de cosseno para um campeao de venda ser promovido. Medido na
# base: a distancia do melhor campeao DA CATEGORIA ao topo fica em 0,0000-0,0316
# nos 14 pares termo x loja; 0,05 cobre todos com folga.
JANELA_SIMILARIDADE = float(os.getenv("JANELA_SIMILARIDADE", "0.05"))
# Grade minima (soma de estoque do produto na loja) para promover um campeao.
# [BLOQUEADO §10.3 item 5] 3 e escolha, nao medida — por isso e env, nao codigo.
RANKING_MINIMO_GRADE = float(os.getenv("RANKING_MINIMO_GRADE", "3"))
# [BLOQUEADO §10.3 item 6] No ATACADO, o revendedor ve primeiro os campeoes de
# venda ("campeao", default) ou os de grade mais funda ("grade")? Em "grade" a
# profundidade reordena DENTRO de cada tier — o escopo de categoria (tier) e
# a ancora semantica continuam mandando, senao o modo viraria "estoque manda".
ATACADO_RANKING = os.getenv("ATACADO_RANKING", "campeao").strip().lower()

# Produtos JA MOSTRADOS a cada cliente, para o "tem mais?" trazer coisa inedita.
# Escopo = CONVERSA (nao turno): "tem mais?" e sempre turno novo, entao escopo de
# turno nao excluiria nada. {user_id: [(id_produto:str, ts), ...]} mais recentes
# no fim, com TTL e teto. Memoria do PROCESSO (exige replicas=1, Gate 0.1).
EXCLUIR_TTL_SEG = float(os.getenv("EXCLUIR_TTL_SEG", "1800"))
EXCLUIR_MAX_IDS = int(os.getenv("EXCLUIR_MAX_IDS", "15"))
EXCLUIR_MAX_USERS = 500
# Piso de resultados abaixo do qual as exclusoes sao abandonadas e a busca refeita.
# Sem isto trocariamos "repetiu os mesmos" por "nao achei nada", que e pior.
EXCLUIR_MIN_RESULTADOS = int(os.getenv("EXCLUIR_MIN_RESULTADOS", "3"))
_mostrados = collections.OrderedDict()
_mostrados_lock = threading.Lock()


def _registrar_mostrados(user_id, ids):
    """Marca `ids` (id_produto) como JA VISTOS por este cliente.

    Chamado no RENDER, um por card efetivamente enviado — o cliente so "viu" o que
    virou card, nao os 8 que a tool devolveu ao modelo. Nunca levanta.
    """
    try:
        if not user_id or not ids:
            return
        agora = time.time()
        with _mostrados_lock:
            reg = _mostrados.get(user_id)
            if reg is None:
                reg = []
                _mostrados[user_id] = reg
            _mostrados.move_to_end(user_id)
            for pid in ids:
                s = str(pid).strip()
                if not s:
                    continue
                # re-mostrar move para o fim (mais recente), nao duplica
                for i, (p, _t) in enumerate(reg):
                    if p == s:
                        reg.pop(i)
                        break
                reg.append((s, agora))
            # trim explicito: `del reg[:-N]` com N=0 nao corta nada (-0 == 0)
            if len(reg) > EXCLUIR_MAX_IDS:
                del reg[:len(reg) - EXCLUIR_MAX_IDS]
            while len(_mostrados) > EXCLUIR_MAX_USERS:
                _mostrados.popitem(last=False)   # descarta o cliente mais antigo
    except Exception as e:
        print(f"[EXCLUIR] falha ao registrar mostrados: {e}")


def _ids_ja_mostrados(user_id):
    """list[str] de id_produto mostrados a ESTE cliente na conversa recente.

    TTL `EXCLUIR_TTL_SEG`, teto `EXCLUIR_MAX_IDS`, mais recentes primeiro.
    GUARD DEFENSIVO: devolve [] em QUALQUER erro e NUNCA levanta. A chamada vive
    dentro do try da busca, cujo `except` responde "Erro ao consultar banco de
    dados" — um NameError/KeyError aqui viraria apagao silencioso de 100% das
    buscas, com diagnostico enganoso.
    """
    try:
        if not user_id:
            return []
        corte = time.time() - EXCLUIR_TTL_SEG
        with _mostrados_lock:
            reg = _mostrados.get(user_id) or []
            vivos = [p for (p, t) in reg if t >= corte]
        return list(reversed(vivos))[:EXCLUIR_MAX_IDS]
    except Exception as e:
        print(f"[EXCLUIR] falha ao ler mostrados: {e}")
        return []


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


def _snapshot_turn_ctx():
    """Copia o contexto do turno para reinstalar em outra thread (ver `_ia_send_com_teto`).

    `excluir_ids` vai pela MESMA referência de lista de propósito: o que a tool
    acumular na thread de trabalho tem de ser visível para quem espera (F5).
    """
    return {
        "user_id": getattr(_turn_ctx, "user_id", None),
        "last_msg": getattr(_turn_ctx, "last_msg", None),
        "msg_cliente": getattr(_turn_ctx, "msg_cliente", None),
        "excluir_ids": getattr(_turn_ctx, "excluir_ids", None),
    }


def _restore_turn_ctx(snap):
    """Reinstala nesta thread o contexto capturado por `_snapshot_turn_ctx`."""
    _turn_ctx.user_id = snap.get("user_id")
    _turn_ctx.last_msg = snap.get("last_msg")
    _turn_ctx.msg_cliente = snap.get("msg_cliente")
    _turn_ctx.excluir_ids = snap.get("excluir_ids") if snap.get("excluir_ids") is not None else []


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


def _log_filtro_evento(**campos):
    """Insere uma linha em tool_filtro_eventos. Falha silenciosa — nunca propaga.

    APPEND-ONLY e com duplicata ESPERADA: o modelo chama a tool varias vezes por
    turno e o retry de 3 tentativas replaya as tool calls. Metrica agrupa por
    turno, nunca conta linhas cruas. `user_id` vem do contexto da THREAD — nunca
    de global de modulo, que vazaria o id de outro cliente.
    """
    campos.setdefault("user_id", _ctx_user_id())
    campos.setdefault("tool", "consultar_estoque_supabase")
    try:
        supabase.table("tool_filtro_eventos").insert(campos).execute()
    except Exception as e:
        print(f"[tool_filtro_eventos] insert falhou: {str(e)[:160]}")


# ================= FOTOS SOB DEMANDA (F3) =================
# O catálogo tem 220 produtos com 2+ fotos (até 10), mas o bot só sabia mandar UMA
# — e sem `.order`, uma qualquer. Aqui nasce a fonte única: `_fotos_do_produto`
# (ordem determinística) + `_fotos_novas` (o que este cliente ainda não recebeu).


def _fotos_pid(pid):
    """Normaliza o id do produto para a chave canônica de string.

    O MESMO cast tem de valer no registro e na leitura: gravar com `int` e ler com
    `str` (ou vice-versa) faz `_fotos_ja_vistas` voltar sempre vazia, e aí a tool
    promete N fotos e o sender manda N-1 (produto de 1 foto prometeria 1 e mandaria
    0). O Gemini manda `54557957.0`, o banco guarda `'54557957'`. Levanta em lixo.
    """
    return str(int(float(pid)))


def _fotos_do_produto(pid):
    """URLs das fotos do produto, em ordem DETERMINÍSTICA (id ASC). [] em erro.

    Por que `id ASC` é a ordem do ERP: `sync_images.py` faz UM insert em lote na
    ordem do array `fotos` vindo do GestaoClick, então o id autoincrement preserva
    essa ordem. `created_at` é idêntico em todas as rows do produto (insert em
    lote) — é inútil para ordenar. Validado olhando 10 produtos amostrados: em
    10/10 a foto de menor id é a foto de FRENTE/apresentação.

    ATENÇÃO: o VALOR do id não pode ser persistido em lugar nenhum como chave —
    `sync_images.py` faz DELETE+INSERT a cada sync e os ids mudam. Ordenar por
    id é estável; guardar id de foto não é. Por isso o registro de "já vistas"
    é chaveado por URL.

    `imagem_url or imagem_mini_url` (nunca as duas): `mini_` é a MESMA foto em
    baixa resolução — 843/843 rows têm mini diferente da full e nenhum mini nulo,
    então mandar as duas duplicaria cada foto.
    """
    try:
        pid = _fotos_pid(pid)
    except (TypeError, ValueError):
        return []
    try:
        r = (supabase.table("produtos_imagens")
             .select("id, imagem_url, imagem_mini_url")
             .eq("produto_id", pid)
             .order("id", desc=False)
             .execute())
        rows = r.data or []
    except Exception as e:
        print(f"[fotos] leitura de produtos_imagens falhou pid={pid}: {str(e)[:140]}")
        return []
    # Cuidado de tipo: produtos_imagens.produto_id é BIGINT e produtos_estoque.id_produto
    # é TEXT. Via PostgREST passar string funciona (o cast é do lado do servidor);
    # nunca escrever SQL cru juntando as duas colunas sem `::text`.
    fotos = []
    for row in rows:
        url = row.get("imagem_url") or row.get("imagem_mini_url")
        if url and url not in fotos:      # dedupe defensivo: não há UNIQUE na tabela
            fotos.append(url)
    return fotos


def _fotos_registry_do_user(user_id):
    """Sub-dicionário {pid: {url: ts}} do user, aplicando LRU. Chamar sob o lock."""
    reg = _fotos_vistas.get(user_id)
    if reg is None:
        reg = {}
        _fotos_vistas[user_id] = reg
    _fotos_vistas.move_to_end(user_id)
    while len(_fotos_vistas) > FOTOS_MAX_USERS_REGISTRY:
        _fotos_vistas.popitem(last=False)      # descarta o cliente mais antigo
    return reg


def _fotos_ja_vistas(user_id, pid):
    """URLs deste produto que ESTE cliente já recebeu na janela de TTL."""
    try:
        pid = _fotos_pid(pid)
    except (TypeError, ValueError):
        return []
    agora = time.time()
    with _fotos_vistas_lock:
        reg = _fotos_registry_do_user(user_id)
        por_url = reg.get(pid) or {}
        vivas = [u for u, ts in por_url.items() if (agora - ts) <= FOTOS_TTL_SEG]
        # expira o que passou do TTL (o cliente volta dias depois e pode rever)
        if len(vivas) != len(por_url):
            reg[pid] = {u: por_url[u] for u in vivas}
        return vivas


def _marcar_fotos_vistas(user_id, pid, urls):
    """Registra que estas URLs JÁ FORAM ENVIADAS a este cliente.

    Chamado (a) no render do card, para a foto principal, e (b) no sender, DEPOIS
    de cada envio bem-sucedido. Marcar depois do envio é de propósito: se a Meta
    recusar a mídia, a foto continua "nova" e sai no próximo pedido.
    """
    try:
        pid = _fotos_pid(pid)
    except (TypeError, ValueError):
        return
    agora = time.time()
    with _fotos_vistas_lock:
        reg = _fotos_registry_do_user(user_id)
        por_url = reg.setdefault(pid, {})
        for u in urls or []:
            if u:
                por_url[u] = agora


def _fotos_novas(user_id, pid):
    """FONTE ÚNICA da tool e do sender: (fotos, vistas, novas).

    A tool só ANUNCIA (read-only) e o sender ENVIA — os dois têm de concordar sobre
    o número, senão a Luna promete uma coisa e o cliente recebe outra. Por isso a
    conta vive aqui, e não duplicada nos dois lugares.
    """
    fotos = _fotos_do_produto(pid)
    vistas = set(_fotos_ja_vistas(user_id, pid))
    novas = [u for u in fotos if u not in vistas]
    return fotos, sorted(vistas), novas


# ================= NOVA FERRAMENTA DE BUSCA (SUPABASE) =================

# Valores que o modelo escreve quando quer dizer "sem tamanho". Nenhum deles existe
# como tamanho real (o catalogo tem 35 valores distintos e nenhum e destes), então
# tratá-los como ausência não esconde produto nenhum.
_TAMANHO_SENTINELA = frozenset({
    "NONE", "NULL", "NIL", "N/A", "NENHUM", "NAO", "NÃO", "UNDEFINED", "NAN",
    "SEM TAMANHO", "NAO INFORMADO", "NÃO INFORMADO", "-", "?", "NAO SE APLICA",
})

# Parametros que so existem na RPC da migration 0013. Se o banco ainda estiver na
# assinatura de 5 args (deploy invertido: codigo antes da migration), o PostgREST
# responde PGRST202 e TODA busca cairia. `_rpc_busca` degrada para os 5 args em vez
# de devolver "Erro ao consultar banco de dados" — o ranking some, a busca fica.
_PARAMS_RPC_0013 = ('excluir_ids', 'termo_tokens', 'limite_produtos',
                    'ancora_semantica', 'janela_similaridade', 'minimo_grade')


def _rpc_busca(rpc_params):
    """Chama a RPC de busca; se a de 0013 nao existir, refaz com os 5 args legados."""
    try:
        return supabase.rpc('buscar_produtos_semantico', rpc_params).execute()
    except Exception as e:
        txt = f"{e}"
        if 'PGRST202' not in txt and 'Could not find the function' not in txt:
            raise
        print("⚠️ [RANKING] RPC 0013 ausente no banco (deploy invertido). "
              "Degradando para a assinatura de 5 args — SEM ranking comercial. "
              "Aplique migrations/0013_rpc_ranking_comercial.sql.")
        legado = {k: v for k, v in rpc_params.items() if k not in _PARAMS_RPC_0013}
        legado['match_count'] = min(int(rpc_params.get('match_count') or 10), 10)
        return supabase.rpc('buscar_produtos_semantico', legado).execute()


def _em_modo_atacado(user_id):
    """True se este cliente ja foi identificado como revendedor nesta execucao."""
    return bool(user_id) and user_id in _atacado_users


def consultar_estoque_supabase(termo_cliente: str, tamanho: str = None, id_loja: str = None):
    """
    Realiza busca semântica no estoque por similaridade vetorial + filtro de tamanho + filtro de loja.

    Args:
        termo_cliente: o que o cliente busca (ex: "fantasia", "camisola algodão", "cueca boxer").
        tamanho: tamanho EXATO que o cliente pediu. Copie literal: se cliente disse "G",
                 passe "G" (NÃO "GG"). Se disse "M", passe "M". Se disse "42", passe "42".
                 Nunca expanda uma letra (G≠GG, M≠MM, P≠PP). Apenas colapse digitação
                 repetida em ≥4 (GGGG → GG). Se o cliente realmente não falou tamanho
                 (ex: produtos sexshop, cosméticos), OMITA este parâmetro — não envie
                 o campo. Nunca escreva a palavra "None"/"null" como valor: ela seria
                 usada como se fosse um tamanho e a busca voltaria vazia.
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
        Cada item traz ainda `tem_foto` (o card sai com imagem) e `n_fotos` (quantas
        fotos existem no total). `n_fotos` maior que 1 significa que há OUTROS ÂNGULOS
        além do card — se o cliente pedir mais fotos, chame `mostrar_fotos_produto`.
        `produtos` traz no máximo 8 itens, um por produto DISTINTO, já ordenados:
        os primeiros são os mais parecidos com o que o cliente pediu; os seguintes
        podem ser campeões de venda da MESMA categoria, marcados com
        `destaque: true`. Ainda assim a lista pode conter item de categoria
        vizinha: leia o nome de CADA produto e descarte o que não for a categoria
        pedida. Entre os produtos da categoria certa, prefira os de
        `destaque: true` — e nunca invente esse selo para os outros.
        Produtos que você já mostrou nesta conversa saem da busca
        automaticamente; `filtro_aplicado.excluir_ids_aplicados` diz quantos.
    """
    print(f"\n[SEMÂNTICO] Buscando: '{termo_cliente}' | Tamanho recebido: {tamanho} | Loja: {id_loja}")

    # 0. Normalização do Tamanho
    tamanho_llm = tamanho.upper().strip() if tamanho else None
    tamanho_alvo = tamanho_llm
    # `tamanho` é declarado como string, então o modelo às vezes escreve a PALAVRA
    # "None" em vez de omitir o campo — e aí a RPC filtrava por tokens ['NONE'],
    # zero linhas, e a Luna dizia "não tenho" para produtos que existiam. Medido em
    # tool_filtro_eventos no primeiro dia: 3 buscas com tamanho_llm='NONE'.
    # `tamanho_llm` guarda o valor cru (é o que a métrica precisa ver).
    if tamanho_alvo in _TAMANHO_SENTINELA:
        print(f"[TAMANHO] LLM mandou a palavra '{tamanho}' como tamanho — tratando como SEM tamanho")
        tamanho_alvo = None

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

    # Base do evento de observabilidade — o mesmo dict alimenta os 4 returns, para
    # que count(*) seja de fato "chamadas da tool" e nenhum denominador saia errado.
    evento = {
        "termo_cliente": termo_cliente,
        "tamanho_llm": tamanho_llm,
        "tamanho_user_tokens": tokens_user_log,
        "tamanho_aplicado": tamanho_alvo,
        "tamanho_aplicado_tokens": sorted(tokens_alvo),
        "guard_acionou": guard_acionou,
        "llm_omitiu_tamanho": llm_omitiu_tamanho,
        "id_loja_aplicado": id_loja_alvo,
    }

    # 1. Gera o vetor da pergunta do cliente
    vetor_busca = get_embedding(termo_cliente)
    if not vetor_busca:
        _log_filtro_evento(status_retornado="erro_embedding", **evento)
        return {"status": "erro", "msg": "Falha na geração do vetor de busca."}

    # Palavras-chave do termo do cliente. Servem a DOIS consumidores: o escopo
    # LEXICAL da tier 1 dentro da RPC (`termo_tokens`) e o desempate por
    # palavra-chave aqui embaixo. Precisam existir ANTES da chamada.
    palavras_chave = set()
    try:
        for palavra in termo_cliente.split():
            p_up = palavra.upper()
            if len(p_up) > 2:
                palavras_chave.add(p_up)
                if p_up.endswith('S'):
                    palavras_chave.add(p_up[:-1])   # "CUECAS" -> "CUECA"
    except Exception as e:
        print(f"⚠️ Erro ao extrair palavras-chave: {e}")

    # Exclusoes: produtos que ESTE cliente ja viu como card na conversa recente.
    # Snapshot CONGELADO em `_executar_turno` antes do laco de retry — as 3
    # tentativas mandam exatamente a mesma lista, senao o retry devolveria
    # resultado diferente da tentativa que falhou (quebra de idempotencia).
    excluir = [str(i) for i in (getattr(_turn_ctx, "excluir_ids", None) or []) if str(i).strip()]
    excluir_aplicados = 0   # hoistado: entra em `filtro_aplicado`, fora do try

    # 2. Chama a RPC 'buscar_produtos_semantico' no Supabase
    try:
        rpc_params = {
            'query_embedding': vetor_busca,
            # Inerte de proposito: o pior match legitimo medido fica em ~0,65.
            # Quem corta o pool e o `match_count` + a ordenacao por distancia.
            'match_threshold': 0.5,
            'match_count': POOL_CANDIDATOS,
            'filtro_tamanho': tamanho_alvo,
            'filtro_id_loja': id_loja_alvo,
            'limite_produtos': LIMITE_PRODUTOS,
            'ancora_semantica': ANCORA_SEMANTICA,
            'janela_similaridade': JANELA_SIMILARIDADE,
            'minimo_grade': RANKING_MINIMO_GRADE,
            'termo_tokens': sorted(palavras_chave) or None,   # escopo lexical da tier 1
            'excluir_ids': excluir or None,
        }
        response = _rpc_busca(rpc_params)
        produtos_candidatos = response.data or []

        # FALLBACK OBRIGATORIO: com exclusoes demais a busca vira "nao achei nada",
        # que e pior do que repetir. Refaz SEM exclusoes e declara isso no retorno.
        excluir_aplicados = len(excluir)
        if excluir and len(produtos_candidatos) < EXCLUIR_MIN_RESULTADOS:
            print(f"[EXCLUIR] fallback sem exclusoes ({len(produtos_candidatos)} < "
                  f"{EXCLUIR_MIN_RESULTADOS} com {len(excluir)} id(s) excluido(s))")
            rpc_params['excluir_ids'] = None
            response = _rpc_busca(rpc_params)
            produtos_candidatos = response.data or []
            excluir_aplicados = 0

        # OTIMIZAÇÃO: Remove o vetor de embedding (muito grande e inútil para a LLM) para economizar tokens
        for p in produtos_candidatos:
            p.pop('embedding', None)

        # 2.1. DESEMPATE POR PALAVRA-CHAVE, DENTRO DO TIER
        # O boost comercial de +10 por `nome_grupo == 'PRODUTOS MAIS VENDIDOS'`
        # saiu daqui de proposito: ele premiava QUALQUER linha do grupo sem olhar
        # categoria, e o prompt manda descartar o que esta fora da categoria
        # pedida — ou seja, promovia exatamente o item que a LLM joga no lixo.
        # Agora quem decide e o `tier` da RPC, que ja e escopado por categoria.
        try:
            for p in produtos_candidatos:
                nome_prod = (p.get('nome') or '').upper()
                p['_score_boost'] = sum(1 for termo in palavras_chave if termo in nome_prod)

            # Sort estavel em 3 niveis: o tier do SQL manda; a palavra-chave
            # desempata DENTRO do tier; e a ordem que o SQL ja deu (grade para
            # tier 1, similaridade para o resto) sobrevive entre iguais.
            # Ordenar por -boost ANTES do tier faria uma palavra-chave promover
            # item tier 2 acima da ancora semantica — o ranking viraria decorativo.
            # `tier` ausente (RPC antiga no ar) -> 0 para todos -> ordem inalterada.
            if ATACADO_RANKING == "grade" and _em_modo_atacado(_ctx_user_id()):
                produtos_candidatos.sort(key=lambda x: (x.get('tier') or 0,
                                                        -float(x.get('estoque_grade') or 0),
                                                        -x.get('_score_boost', 0)))
            else:
                produtos_candidatos.sort(key=lambda x: (x.get('tier') or 0,
                                                        -x.get('_score_boost', 0)))
            print(f"[RANKING] tiers={[p.get('tier') for p in produtos_candidatos]} "
                  f"palavras-chave={sorted(palavras_chave)}")

        except Exception as e:
            print(f"⚠️ Erro no desempate por palavra-chave: {e}")

        # 2.2. BUSCA DE IMAGENS — TODAS as fotos, em ordem determinística
        # `.order("id")` é obrigatório: sem ele o PostgREST devolve as rows em ordem
        # física (não garantida) e a foto do card mudava de execução para execução.
        # Com id ASC a foto principal é sempre a primeira do ERP (validado em 10
        # produtos: 10/10 a de menor id é a de frente). `n_fotos` diz ao modelo
        # quantos ângulos existem, para ele poder oferecer `mostrar_fotos_produto`.
        ids_candidatos = [p['id_produto'] for p in produtos_candidatos]
        if ids_candidatos:
            try:
                res_imgs = (supabase.table("produtos_imagens")
                            .select("produto_id, imagem_url, imagem_mini_url")
                            .in_("produto_id", ids_candidatos)
                            .order("id", desc=False)
                            .execute())
                mapa_fotos = {}
                for img in res_imgs.data or []:
                    pid = str(img['produto_id'])   # chave string: id_produto é TEXT
                    url = img.get('imagem_url') or img.get('imagem_mini_url')
                    if not url:
                        continue
                    lista = mapa_fotos.setdefault(pid, [])
                    if url not in lista:           # dedupe (não há UNIQUE na tabela)
                        lista.append(url)

                for p in produtos_candidatos:
                    fotos = mapa_fotos.get(str(p['id_produto'])) or []
                    p['imagem'] = fotos[0] if fotos else None
                    p['n_fotos'] = len(fotos)
                    # A LISTA de URLs NÃO entra no payload do modelo: custo de token
                    # e o §0 do prompt proíbe URL na resposta. O sender relê fresco.
            except Exception as e:
                print(f"⚠️ Erro ao buscar imagens: {e}")


        for i, p in enumerate(produtos_candidatos):
            print(f"  {i+1}. {p.get('nome')} | T:{p.get('tamanho')} | R$ {p.get('preco')}")
    except Exception as e:
        print(f"❌ Erro RPC: {e}")
        _log_filtro_evento(status_retornado="erro_rpc", **evento)
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
        # Quantos produtos ja mostrados foram EXCLUIDOS desta busca. 0 quando nao
        # havia nada a excluir OU quando o fallback abandonou as exclusoes para
        # nao devolver lista vazia — nos dois casos a lista pode repetir o que o
        # cliente ja viu, e o modelo tem de saber disso.
        "excluir_ids_aplicados": excluir_aplicados,
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

    evento.update({
        "n_candidatos": len(produtos_candidatos or []),
        "n_validados": len(validados),
        "n_dropados_overlap": len(dropados),
        "n_dropados_igualdade_legado": n_dropados_igualdade_legado,
        "tamanhos_dropados": [str(t) for t in dropados if t],
    })

    if not validados:
        _log_filtro_evento(status_retornado="vazio", **evento)
        return {"status": "vazio", "filtro_aplicado": filtro_aplicado,
                **({"aviso": aviso} if guard_acionou else {}),
                "msg": (f"Nenhum produto com estoque para o termo buscado no tamanho "
                        f"{tamanho_alvo} na loja {id_loja_alvo}. O filtro foi aplicado "
                        f"literalmente; nao conclua nada sobre outro tamanho sem chamar "
                        f"a ferramenta de novo.")}

    # 4. Corte final: um produto DISTINTO por linha, preservando a ordem do ranking.
    # A chave é `id_produto`, não `id_unico`: `id_unico` é a PK (produto+tamanho+loja),
    # então dedupar por ela era inerte e a lista saía com o mesmo produto 2-4 vezes
    # em tamanhos diferentes — 10 linhas viravam 3-6 produtos.
    # Efeito colateral bom: `extrair_produtos_de_tool_results` indexa por
    # `int(id_produto)` com last-write-wins; com 1 linha por produto o cache do
    # card passa a ser determinístico. A RPC (0013) já deduplica, isto é a rede de
    # segurança para o caso da RPC antiga estar no ar.
    vistos = set()
    selecao = []
    for p in validados:
        chave = str(p.get('id_produto') or '').strip()
        if chave and chave not in vistos:
            vistos.add(chave)
            selecao.append(p)
        if len(selecao) >= LIMITE_PRODUTOS:
            break

    # Sinal explícito de foto p/ o modelo: evita prometer imagem que o sistema
    # não vai enviar (card sem imagem sai só como texto — renderizar_mensagem_estruturada).
    # `n_fotos` é normalizado AQUI (fora do try da busca de imagens) para que uma
    # falha na leitura de produtos_imagens não deixe a chave ausente no payload.
    for p in selecao:
        p['n_fotos'] = int(p.get('n_fotos') or 0)
        p['tem_foto'] = bool(p.get('imagem'))

    # 4.1. Payload enxuto. Os campos internos do ranking vão para o LOG, não para o
    # Gemini: `nome_grupo` vazaria rótulo do ERP, `similarity`/`tier`/`estoque_grade`
    # são ruído caro (o custo de token é por chamada de tool, em todo turno de busca).
    # `destaque` (booleano) carrega a mesma informação comercial e agora significa
    # "promovido DENTRO da categoria pedida", não "pertence ao grupo do ERP".
    for p in selecao:
        print(f"  [RANK] tier={p.get('tier')} destaque={p.get('destaque')} "
              f"grade={p.get('estoque_grade')} sim={p.get('similarity')} "
              f"n_tam={p.get('n_tamanhos')} | {p.get('nome')}")
    for p in selecao:
        for k in ('id_loja', 'loja', 'grupo_id', 'nome_grupo', 'similarity',
                  'tier', '_score_boost', 'estoque_grade', 'n_tamanhos'):
            p.pop(k, None)
        # `destaque` só existe na RPC 0013; sem ela a chave tem de existir mesmo
        # assim, senão o modelo veria o campo em uns itens e não em outros.
        p['destaque'] = bool(p.get('destaque'))

    print(f"[SEMÂNTICO] Retornando {len(selecao)} produtos distintos "
          f"({sum(1 for p in selecao if p['destaque'])} em destaque).")
    # NAO acrescentar chaves novas a `evento`: ele vira insert direto em
    # tool_filtro_eventos e uma coluna inexistente derruba o insert INTEIRO
    # (falha silenciosa) — perderiamos a observabilidade da F2 sem aviso.
    _log_filtro_evento(status_retornado="sucesso", **evento)
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
        {status, produto: {id_produto, nome, nome_grupo, imagem, tem_foto, n_fotos,
         variacoes: [{id_unico, tamanho, preco_varejo, preco_atacado, estoque, loja}]}}
        `tem_foto` diz se o card sai com imagem; `n_fotos` é o total de fotos do
        produto. `n_fotos` maior que 1 significa que há OUTROS ÂNGULOS além do card —
        se o cliente pedir mais fotos, chame `mostrar_fotos_produto`.
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
    # Esta tool era CEGA a foto: o produto revalidado por id voltava sem `imagem`,
    # o cache gravava None e o card do mesmo produto saía como texto puro — o
    # cliente "perdia" a foto que já tinha visto. Uma query extra indexada
    # (idx_produtos_imagens_produto_id), no máximo 10 rows.
    fotos = _fotos_do_produto(id_produto_normalizado)
    return {
        "status": "sucesso",
        "produto": {
            "id_produto": base["id_produto"],
            "nome": base.get("nome"),
            "nome_grupo": base.get("nome_grupo"),
            "imagem": fotos[0] if fotos else None,
            "tem_foto": bool(fotos),
            "n_fotos": len(fotos),
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


class _FotosPendentes:
    """Coletor dos pedidos de foto do turno — a ponte entre a tool e o sender.

    Por que uma classe e não uma lista: a thread de trabalho do Gemini
    (`_ia_send_com_teto`) é ABANDONADA de propósito quando o orçamento do turno
    estoura, e as tools dela JÁ RODARAM — ela pode acordar depois do fallback e
    chamar `mostrar_fotos_produto` de novo. `fechar()` fecha a janela de uma vez:
    todo `add` posterior é ignorado, então a thread zumbi não consegue disparar
    envio de foto para um turno que já terminou. Uma lista nua não sabe dizer
    "não" — `clear()` + append tardio reabriria a janela.

    Só o pid entra aqui, nunca URL: as URLs são relidas frescas na hora do envio.
    """

    __slots__ = ("_pids", "_fechado", "_lock")

    def __init__(self):
        self._pids = []
        self._fechado = False
        self._lock = threading.Lock()

    def add(self, pid):
        """True se o pid entrou agora; False se já estava, ou se a janela fechou."""
        with self._lock:
            if self._fechado or pid in self._pids:
                return False
            self._pids.append(pid)
            return True

    def fechar(self):
        """Drena e FECHA. Idempotente: a 2ª chamada devolve [] — é o que garante
        que o sender só possa enviar uma vez, mesmo se for chamado duas vezes."""
        with self._lock:
            self._fechado = True
            pids, self._pids = self._pids, []
            return pids

    def __contains__(self, pid):
        with self._lock:
            return pid in self._pids

    def __len__(self):
        with self._lock:
            return len(self._pids)

    def __bool__(self):
        return len(self) > 0


def criar_tool_mostrar_fotos(user_id, pendentes):
    """Retorna a tool de fotos amarrada ao user_id e ao coletor DESTE turno.

    Closure no molde de `criar_tool_transferir`: nada de global de módulo nem de
    leitura do contexto de thread. É o que faz a tool funcionar de dentro da thread
    de trabalho de `_ia_send_com_teto` sem depender de `_restore_turn_ctx`.
    """

    def mostrar_fotos_produto(id_produto: int):
        """
        Registra que o cliente quer VER MAIS FOTOS de um produto. O sistema envia as
        fotos automaticamente DEPOIS da sua resposta — você não envia nada.

        Use quando o cliente pedir mais imagens de uma peça específica: "manda mais
        fotos", "tem outra foto?", "quero ver melhor", "mostra de outro ângulo".
        Não use para o primeiro card de um produto (o card já vai com a foto
        principal); use quando ele quiser ver ALÉM do card.

        REGRAS DE LINGUAGEM (obrigatórias):
        - NUNCA escreva URL de imagem na sua resposta.
        - NUNCA diga qual ângulo/lado é a foto ("aqui a de trás", "essa é a frente"):
          o sistema não sabe o que cada foto mostra. Fale sempre de forma neutra —
          "outros ângulos que eu tenho dessa peça".
        - NUNCA afirme a cor de uma foto. Fotos do mesmo produto podem ser de outra
          cor ou de outra estampa.
        - Anuncie no máximo o que `vai_enviar` diz. Não prometa número diferente.
        - NUNCA diga que uma atendente vai TIRAR ou MANDAR uma foto: você não sabe
          se ela vai. Se for chamar atendente, diga que ela ajuda com os detalhes
          da peça.

        Args:
            id_produto: o id numérico do produto (campo id_produto).

        Returns:
            dict com `status`:
            - "ok": o sistema VAI enviar `vai_enviar` foto(s) depois da sua resposta.
              Anuncie de forma neutra. Se `restantes` for maior que 0, ainda sobram
              fotos: se o cliente pedir mais, chame esta ferramenta de novo no
              próximo turno.
            - "sem_foto": este produto não tem foto nenhuma cadastrada. Seja
              transparente ("ainda não temos foto desse item aqui no sistema"),
              não prometa imagem e NÃO ofereça atendente por causa da foto.
            - "sem_novas": o cliente JÁ RECEBEU TODAS as fotos que existem. Diga que
              essas são todas que você tem. SÓ neste caso você pode oferecer uma
              atendente — e para ajudar com os DETALHES da peça, nunca prometendo
              que ela vai tirar ou mandar foto nova.
            - "limite_turno": você já pediu as fotos de outro produto nesta resposta.
              Trate um produto por vez; peça o outro no próximo turno.
            - "erro": id inválido.
        """
        try:
            pid = _fotos_pid(id_produto)
        except (TypeError, ValueError):
            return {"status": "erro", "msg": f"id_produto invalido: {id_produto!r}"}

        fotos, vistas, novas = _fotos_novas(user_id, pid)
        nome = _nome_do_produto(pid)

        if not fotos:
            print(f"[fotos] tool pid={pid}: sem_foto")
            return {"status": "sem_foto", "id_produto": pid, "nome": nome, "n_fotos": 0,
                    "msg": "Esse produto nao tem foto cadastrada. Seja transparente e "
                           "nao prometa imagem."}
        if not novas:
            print(f"[fotos] tool pid={pid}: sem_novas (n_fotos={len(fotos)})")
            return {"status": "sem_novas", "id_produto": pid, "nome": nome,
                    "n_fotos": len(fotos), "ja_vistas": len(vistas), "vai_enviar": 0,
                    "msg": "O cliente ja recebeu TODAS as fotos deste produto. Diga que "
                           "sao todas que voce tem e ofereca uma atendente para ajudar "
                           "com os DETALHES da peca — nunca prometa que ela vai tirar "
                           "ou mandar uma foto nova."}

        # Um produto por turno: a tool NAO PODE prometer mais do que o sender manda,
        # e o orcamento de envio do turno e o mesmo do produto (FOTOS_MAX_POR_TURNO).
        if pid not in pendentes and len(pendentes) >= 1:
            print(f"[fotos] tool pid={pid}: limite_turno")
            return {"status": "limite_turno", "id_produto": pid, "nome": nome,
                    "n_fotos": len(fotos), "vai_enviar": 0,
                    "msg": "Voce ja pediu fotos de outro produto nesta resposta. Trate um "
                           "produto por vez; peca este no proximo turno."}

        # Idempotência por construção: a tool é READ-ONLY (não envia e não escreve em
        # `_fotos_vistas`) e o `add` dedupa. Logo o replay do retry de 3 tentativas e
        # as chamadas repetidas que o modelo faz por decisão própria são inofensivas:
        # todas devolvem o MESMO payload e o pid entra uma única vez.
        novo = pendentes.add(pid)
        vai_enviar = min(len(novas), FOTOS_MAX_POR_PEDIDO)
        if not novo and pid not in pendentes:
            # Janela fechada (turno já terminou e a thread abandonada acordou).
            # Não pode prometer envio: ninguém vai ler este coletor.
            print(f"[fotos] tool pid={pid}: janela do turno FECHADA — pedido ignorado")
            return {"status": "limite_turno", "id_produto": pid, "nome": nome,
                    "n_fotos": len(fotos), "vai_enviar": 0,
                    "msg": "Nao foi possivel agendar o envio agora; peca de novo."}
        print(f"[fotos] tool pid={pid}: ok n_fotos={len(fotos)} ja_vistas={len(vistas)} "
              f"vai_enviar={vai_enviar} novo_no_turno={novo}")
        if len(fotos) == 1:
            # Produto de UMA foto (216 no catálogo): não existe "outro ângulo". Se o
            # modelo anunciasse ângulos no plural mentiria — e se o card desta mesma
            # resposta já levar essa foto, o sender não manda nada (a foto já foi).
            msg = ("Este produto tem UMA foto so, que e a mesma do card. Diga que e a "
                   "unica foto que voce tem dessa peca e ofereca uma atendente para os "
                   "detalhes — sem prometer foto nova. Nunca fale de angulos no plural.")
        else:
            msg = ("O sistema vai enviar as fotos DEPOIS da sua resposta. Anuncie de "
                   "forma neutra ('outros angulos que eu tenho'), sem dizer qual lado "
                   "e sem afirmar cor, e sem escrever URL.")
        return {
            "status": "ok",
            "id_produto": pid,
            "nome": nome,
            "n_fotos": len(fotos),
            "ja_vistas": len(vistas),
            "vai_enviar": vai_enviar,
            "restantes": max(0, len(novas) - vai_enviar),
            "msg": msg,
        }

    return mostrar_fotos_produto


def _nome_do_produto(pid):
    """Nome do produto para a tool de fotos poder citar a peça. None se não achar."""
    try:
        r = (supabase.table("produtos_estoque").select("nome")
             .eq("id_produto", _fotos_pid(pid)).limit(1).execute())
        rows = r.data or []
        return rows[0].get("nome") if rows else None
    except Exception as e:
        print(f"[fotos] nome do produto {pid}: {str(e)[:120]}")
        return None


def _legenda_fotos_extra(pid, n_envio, n_total):
    """Legenda da 1ª foto extra. NUNCA nomeia o ângulo nem afirma cor.

    Decisão do dono da loja: não identificamos "parte de trás" (não existe dado de
    ordem/tipo/cor em produtos_imagens, e há produtos cuja 2ª foto é de outra
    estampa) — a linguagem é sempre "outros ângulos que eu tenho".

    `n_total` é obrigatório porque produto de UMA foto não tem "outro ângulo": a
    legenda genérica mentiria justamente no maior bucket do catálogo (216 produtos).
    O marcador `_cód: {pid}_` é o MESMO do card (acentuado), para `card_envios.legenda`
    e o reply-to-card ficarem consistentes.
    """
    if n_total <= 1:
        titulo = "Essa é a única foto que eu tenho dessa peça 💕"
    elif n_envio <= 1:
        titulo = "Aqui outro ângulo que eu tenho dessa peça 💕"
    else:
        titulo = "Aqui os outros ângulos que eu tenho dessa peça 💕"
    return f"{titulo}\n_cód: {int(pid)}_"


def _enviar_fotos_extras_pendentes(user_id, pendentes):
    """Envia as fotos extras pedidas neste turno. Roda DEPOIS da resposta do modelo.

    Ordem importa: o texto do modelo ("já te mando outros ângulos") tem de chegar
    antes das imagens. Roda ainda DENTRO do `turn_lock` — fora dele a mídia deste
    turno se intercalaria com os cards do turno seguinte. Como o pop do buffer
    acontece no INÍCIO do turno (F1), a mensagem que chegar durante estes segundos
    entra num buffer novo e espera no lock; nada se perde.

    Três guardas de idempotência, independentes:
      1. `pendentes.fechar()` drena e fecha — 2ª chamada não tem o que enviar;
      2. `_fotos_novas` relê o registro antes de cada produto;
      3. `_marcar_fotos_vistas` grava URL por URL DEPOIS do envio — uma foto já
         enviada nunca é candidata de novo (nem em outro turno, dentro do TTL).
    """
    pids = pendentes.fechar()
    if not pids:
        return 0
    orcamento = FOTOS_MAX_POR_TURNO
    enviadas = 0
    for pid in pids:
        if orcamento <= 0:
            break
        fotos, vistas, novas = _fotos_novas(user_id, pid)
        if not novas:
            print(f"[fotos] pid={pid}: nada novo para enviar (n_fotos={len(fotos)})")
            continue
        escolhidas = novas[:min(FOTOS_MAX_POR_PEDIDO, orcamento)]
        anunciou = False        # a legenda de anúncio vai na 1ª foto que SAIU de fato:
                                # se a primeira for recusada, o anúncio iria junto com
                                # ela e o cliente receberia só imagens com `_cód:`.
        for url in escolhidas:
            legenda = (f"_cód: {int(pid)}_" if anunciou     # nunca legenda vazia: o
                       else _legenda_fotos_extra(pid, len(escolhidas), len(fotos)))
                                                    # payload cloud sempre manda `caption`
            try:
                wamid = enviar_midia_whatsapp(user_id, url, legenda)
            except Exception as e:
                print(f"[fotos] envio de {url[-16:]} falhou: {str(e)[:120]}")
                continue
            # No provider cloud, `None` significa que a Meta recusou a mídia. Nesse
            # caso NÃO marcar como vista: senão o cliente nunca recebe a foto e o bot
            # passa a achar que recebeu (e diria "são todas que eu tenho"). No uazapi
            # a função devolve None mesmo em sucesso, então não há sinal — marca.
            if WHATSAPP_PROVIDER == "cloud" and wamid is None:
                print(f"[fotos] provider recusou {url[-16:]} — segue como foto NOVA")
                continue
            # isinstance(str) é obrigatório: o stub da suíte devolve dict.
            if WHATSAPP_PROVIDER == "cloud" and isinstance(wamid, str):
                registrar_card_enviado(wamid, user_id, int(pid), legenda)
            _marcar_fotos_vistas(user_id, pid, [url])
            anunciou = True
            enviadas += 1
            orcamento -= 1
            if FOTOS_SLEEP_SEG:
                time.sleep(FOTOS_SLEEP_SEG)
    # `tool_chamada` mede a taxa de acionamento: sem o bloco de prompt (F6) a tool
    # depende só da docstring, e é este log que diz se o modelo a está usando.
    print(f"[fotos] tool_chamada={1 if pids else 0} pids={pids} extras_enviadas={enviadas}")
    return enviadas


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
            # `mostrar_fotos_produto` fica FORA desta tupla de propósito: se o pid dela
            # entrasse no cache e o modelo o listasse em `produtos_recomendados`, o
            # renderizador reenviaria o CARD com a foto principal — foto duplicada.
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
                    # Era None fixo — e por isso o card de um produto revalidado por
                    # id saía SEM foto. Agora `consultar_produto_por_id` devolve
                    # `imagem` (F3) e o cache a repassa.
                    "imagem": produto.get('imagem'),
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
            # A foto do card conta como JÁ VISTA: sem isto o pedido de "mais fotos"
            # reenviaria exatamente a imagem que o cliente acabou de receber.
            # Exceto se o provider cloud recusou a mídia (wamid None): aí o cliente
            # não viu nada e a foto tem de continuar candidata.
            if not (WHATSAPP_PROVIDER == "cloud" and wamid is None):
                _marcar_fotos_vistas(user_id, pid, [url])
        else:
            time.sleep(0.5)
            wamid = enviar_mensagem_whatsapp(user_id, legenda)
        if WHATSAPP_PROVIDER == "cloud" and isinstance(wamid, str):
            registrar_card_enviado(wamid, user_id, int(pid), legenda)
        # O cliente VIU este produto: sai das proximas buscas da conversa, para que
        # "tem mais?" traga coisa inedita. Registrado por CARD (nao pelos 8 que a
        # tool devolveu) — so o card chega ao cliente. Nunca levanta.
        _registrar_mostrados(user_id, [pid])
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


# Teto de tempo TOTAL do turno (todas as tentativas somadas).
# Por que existe: `request_options={"timeout": 45}` e um pedido ao SDK, nao uma
# garantia — ja aconteceu de uma chamada ficar ~11 h pendurada sem devolver erro.
# Depois da F1 isso e pior: o turno pendurado segura o `turn_lock` daquele cliente,
# e todas as mensagens seguintes dele ficam presas na fila para sempre.
# Por que 150 s: o teto e uma REDE DE SEGURANCA, nao um encurtador do retry. O pior
# caso legitimo das 3 tentativas e 45 + 2 + 45 + 4 + 45 = 141 s (timeout do SDK mais
# os sleeps de 2·tentativa); 150 cobre isso com ~9 s de folga para start_chat e
# get_history dentro da janela. Calibrado com dado: na suite, um turno real chegou a
# 120,4 s e caiu no fallback com 120 — ou seja, um teto de 120 TRUNCA a 3a tentativa
# (ela receberia so 24 s) e desfaz o retry que o commit 5e4f727 introduziu.
# O ganho segue sendo o que importa: o pior caso do cliente vai de HORAS para ~2,5 min.
TURNO_ORCAMENTO_S = float(os.getenv("TURNO_ORCAMENTO_S", "150"))
IA_TIMEOUT_S = float(os.getenv("IA_TIMEOUT_S", "45"))
_IA_TENTATIVA_MIN_S = 5.0     # abaixo disto nao vale abrir mais uma tentativa


class TurnoOrcamentoEsgotado(Exception):
    """O turno estourou o teto de tempo TOTAL. Cai no fallback gracioso."""


def _ia_send_com_teto(chat, texto, teto_s):
    """`chat.send_message` com teto de tempo de PAREDE, custe o que custar.

    Roda numa thread daemon e desiste de esperar em `teto_s`. Se a chamada nao
    voltou, a thread e ABANDONADA (nao ha como cancelar um socket do SDK) e o
    turno segue para o fallback — o importante e liberar o `turn_lock`.
    O contexto do turno e reinstalado na thread de trabalho: as tools rodam la
    dentro (automatic function calling) e sem isto o guard de tamanho e o
    `user_id` de `tool_filtro_eventos` ficariam cegos.
    A thread abandonada nao envia nada ao cliente — o render acontece aqui fora.
    """
    caixa = {}
    snap = _snapshot_turn_ctx()
    timeout_sdk = max(1.0, min(IA_TIMEOUT_S, teto_s))

    def _worker():
        _restore_turn_ctx(snap)
        try:
            caixa["r"] = chat.send_message(texto, request_options={"timeout": timeout_sdk})
        except BaseException as e:      # noqa: BLE001 — re-levantada na thread chamadora
            caixa["e"] = e
        finally:
            _clear_turn_ctx()

    th = threading.Thread(target=_worker, daemon=True, name="ia-send")
    th.start()
    th.join(timeout=teto_s)
    if th.is_alive():
        raise TurnoOrcamentoEsgotado(
            f"orcamento do turno esgotado: chat.send_message nao retornou em "
            f"{teto_s:.0f}s — thread abandonada, turn_lock liberado")
    if "e" in caixa:
        raise caixa["e"]
    return caixa.get("r")


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
        # Coletor dos pedidos de foto deste turno (F3). A tool só ANOTA aqui; o envio
        # acontece depois, uma vez, abaixo.
        fotos_pendentes = _FotosPendentes()
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
        # Fotos extras DEPOIS da resposta e ainda DENTRO do turn_lock: o cliente lê o
        # anúncio antes das imagens e nada se intercala com o turno seguinte.
        # Chamado UMA vez por turno; `fechar()` lá dentro garante que uma segunda
        # chamada (por qualquer caminho) não envie nada.
        if turno_ok and FOTOS_SOB_DEMANDA and fotos_pendentes:
            try:
                _enviar_fotos_extras_pendentes(user_id, fotos_pendentes)
            except Exception as e:
                print(f"[fotos] envio extra falhou: {e}")
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

        _tools = [
            consultar_estoque_supabase,
            consultar_produto_por_id,
            calcular_total,
            verificar_promocao_hoje,
            transferir_para_atendente,
            calcular_frete_estimado,
        ]
        # Kill switch da F3: com FOTOS_SOB_DEMANDA=0 a tool nem aparece na function
        # declaration e o sender vira no-op — rollback sem redeploy.
        # `fotos_pendentes is None` = chamador que não passa coletor (testes que
        # chamam `_executar_turno` com 2 args): sem coletor o sender não teria como
        # saber o que enviar, então a tool também não entra.
        if FOTOS_SOB_DEMANDA and fotos_pendentes is not None:
            _tools.append(criar_tool_mostrar_fotos(user_id, fotos_pendentes))

        # Tenta com response_schema; se a versão do SDK / modelo recusar
        # generation_config + tools, recria sem schema (cai no fallback regex).
        modelo_args = dict(
            model_name='gemini-3-flash-preview',
            tools=_tools,
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

        # SNAPSHOT CONGELADO dos "ja mostrados", ANTES do laco de retry: as 3
        # tentativas tem de mandar exatamente a mesma lista de exclusoes, senao a
        # 2a tentativa devolveria um conjunto diferente da 1a e o turno deixaria de
        # ser idempotente. Mutacao IN-PLACE de proposito: `_snapshot_turn_ctx`
        # entrega a MESMA referencia de lista a thread de trabalho onde as tools
        # rodam — rebindar aqui e inofensivo (o snapshot vem depois), mas manter a
        # referencia deixa a porta aberta para a tool escrever de volta.
        _ja_vistos = _ids_ja_mostrados(user_id)
        _lst_excluir = getattr(_turn_ctx, "excluir_ids", None)
        if isinstance(_lst_excluir, list):
            _lst_excluir[:] = _ja_vistos
        else:
            _turn_ctx.excluir_ids = list(_ja_vistos)
        if _ja_vistos:
            print(f"[EXCLUIR] {len(_ja_vistos)} produto(s) ja mostrado(s) sairao da busca: {_ja_vistos}")

        t_inicio = time.perf_counter()
        # Gemini as vezes trava/estoura o deadline (504), sobretudo em turns com cadeia
        # de tools (ex.: resposta-a-card -> consultar_estoque + calcular_total). Sem retry
        # o cliente caia no fallback "reenvie" e a Luna parecia muda. Retry com o chat
        # reconstruido a cada tentativa (history vem do banco e e estavel).
        # O retry tem ORCAMENTO GLOBAL: cada tentativa so recebe o que sobrou do teto
        # do turno. Sem isso um unico send_message pendurado prende o turn_lock deste
        # cliente indefinidamente (medido: ~11 h na 3a tentativa).
        response = None
        _MAX_TENTATIVAS_IA = 3
        chat = None
        for _tentativa in range(1, _MAX_TENTATIVAS_IA + 1):
            _restante = TURNO_ORCAMENTO_S - (time.perf_counter() - t_inicio)
            if _restante < _IA_TENTATIVA_MIN_S:
                raise TurnoOrcamentoEsgotado(
                    f"orcamento do turno ({TURNO_ORCAMENTO_S:.0f}s) esgotado antes da "
                    f"tentativa {_tentativa}")
            chat = model.start_chat(history=history, enable_automatic_function_calling=True)
            try:
                response = _ia_send_com_teto(chat, texto_completo, _restante)
                break
            except TurnoOrcamentoEsgotado:
                raise                    # teto e global: nao ha o que retentar
            except Exception as e_ia:
                _restante = TURNO_ORCAMENTO_S - (time.perf_counter() - t_inicio)
                _espera = 2 * _tentativa
                if (_tentativa < _MAX_TENTATIVAS_IA and _erro_ia_transitorio(e_ia)
                        and _restante > _espera + _IA_TENTATIVA_MIN_S):
                    print(f"⚠️ IA transitorio (tentativa {_tentativa}/{_MAX_TENTATIVAS_IA}): "
                          f"{type(e_ia).__name__}: {str(e_ia)[:120]} — retry "
                          f"(restam {_restante:.0f}s do orcamento)")
                    time.sleep(_espera)
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
        # O turno morreu: NÃO mandar as fotos extras. O anúncio ("já te mando outros
        # ângulos") nunca chegou ao cliente — ele receberia imagens soltas depois de
        # um "reenvie sua mensagem". `fechar()` também trava a janela contra a thread
        # abandonada, que pode acordar depois deste ponto e chamar a tool de novo.
        if fotos_pendentes is not None:
            descartados = fotos_pendentes.fechar()
            if descartados:
                print(f"[fotos] turno falhou — {len(descartados)} pedido(s) de foto descartado(s): {descartados}")
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
# Limiar 20 = folga para falha transitoria de embedding. Depois que o re-embed
# total curar a base o regime normal e 0: o ramo de re-embed do sync_erp repara
# sozinho a cada ciclo. Ver POLITICA_DE_VETORIZACAO.md.
EMB_NULL_ALERT_MAX = int(os.getenv("EMB_NULL_ALERT_MAX", "20"))
_emb_alert_state = {"alerting": False, "last_alert_at": None}
_emb_health_cache = {"at": None, "data": {}}


def _sync_age_min():
    """Retorna (idade_min, last_run_dt|None, tolerancia_min) usando o heartbeat do sync_erp."""
    now = datetime.now(timezone.utc)
    last = sync_erp.LAST_RUN_AT
    if last is None:
        # Ainda nao rodou desde o boot: tolera boot + 1 ciclo antes de considerar parado.
        return (now - PROCESS_START_AT).total_seconds() / 60.0, None, SYNC_STALE_ALERT_MIN + 15
    return (now - last).total_seconds() / 60.0, last, SYNC_STALE_ALERT_MIN


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
        # ALARME: `nulos_com_estoque` > 0 = produto com estoque INVISIVEL para a
        # busca semantica (a RPC descarta linha sem vetor). Regime normal e 0.
        "embeddings": _embeddings_health(),
        # Do ultimo ciclo do sync: quantas linhas nulas ele viu e quantas reparou.
        # None ate o primeiro ciclo, e sempre None se o cron dedicado for ligado
        # (a metrica real continua vindo de "embeddings", que independe do processo).
        "sync_emb_nulos_inicio": sync_erp.LAST_RUN_EMB_NULOS,
        "sync_reembeds": sync_erp.LAST_RUN_REEMBEDS,
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
        # Sem a 2a condicao o guard de zeragem do sync_erp nasce MUDO: um ciclo
        # que abortou a zeragem por falha de pagina do ERP e RECENTE, nao velho,
        # e passaria batido por um alarme que so olha idade.
        degradado = (sync_erp.LAST_RUN_OK is False)
        if age > tol or degradado:
            la = _alert_state["last_alert_at"]
            reenvio = la is None or (now - la).total_seconds() > 3 * 3600
            if not _alert_state["alerting"] or reenvio:
                corpo = (
                    "O sync de estoque do bot Sangali parece parado.\n"
                    f"Ultima execucao: {last.isoformat() if last else 'nenhuma desde o boot'}\n"
                    f"Ha ~{int(age)} min (limite {int(tol)} min).\n"
                    f"Ultimo ciclo terminou {'DEGRADADO' if degradado else 'ok'}: {sync_erp.LAST_RUN_INFO}\n"
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
    scheduler.add_job(_verificar_embeddings_e_alertar, 'interval', minutes=60,
                      misfire_grace_time=600, coalesce=True, max_instances=1)
    scheduler.start()
    print("Schedulers iniciados: Imagens diario 6h BRT | Purge bot_turns diario 4h BRT")

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)