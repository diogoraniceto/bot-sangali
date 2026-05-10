import os
import re
import json
import requests
import time
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPICallError
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from sync_erp import sync_otimizado
from sync_images import sync_images

# ================= CONFIGURAÇÕES =================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
UAZAPI_URL = os.getenv("UAZAPI_URL")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN")
MELHOR_ENVIO_TOKEN = os.getenv("MELHOR_ENVIO_TOKEN")
MELHOR_ENVIO_URL = "https://www.melhorenvio.com.br/api/v2/me/shipment/calculate"

app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

message_buffers = {}
_message_buffers_global_lock = threading.Lock()  # protege escrita no dict global
_user_locks = {}  # threading.Lock() por user_id (criado on-demand)

# Última mensagem do user por user_id, usada para validar tamanho contra LLM drift.
# Quando LLM passa um tamanho diferente do que o user disse, forçamos o do user.
_user_last_msg = {}

# user_id "ativo" do turn atual — usado pelas tools para acessar contexto da request
# sem precisar receber user_id como argumento. Setado em process_and_respond.
_consultar_estoque_active_user_id = None

# Rate limit: histórico de timestamps por user_id. {user_id: [ts1, ts2, ...]}
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_MSGS = 10
_rate_limit_history = {}


def _get_user_lock(user_id):
    """Retorna o Lock dedicado a este user_id (cria se não existir)."""
    with _message_buffers_global_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


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

# ================= AUXILIARES DE INTELIGÊNCIA =================

def get_embedding(text):
    """Gera o vetor semântico para a busca no banco."""
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=text,
            task_type="retrieval_query",
            output_dimensionality=768
        )
        return result['embedding']
    except Exception as e:
        print(f"❌ Erro Embedding: {e}")
        return None

# ================= PERSISTÊNCIA (SUPABASE) =================

def save_message(user_id, role, content):
    supabase.table("chat_history").insert({
        "user_id": user_id,
        "role": role,
        "content": content
    }).execute()

def get_history(user_id, limit=30):
    response = supabase.table("chat_history") \
        .select("role, content") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(limit) \
        .execute()

    history = []
    for msg in reversed(response.data):
        history.append({"role": msg["role"], "parts": [msg["content"]]})
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

def consultar_estoque_supabase(termo_cliente: str, tamanho: str = None):
    """
    Realiza busca semântica no estoque por similaridade vetorial + filtro de tamanho.

    Args:
        termo_cliente: o que o cliente busca (ex: "fantasia", "camisola algodão", "cueca boxer").
        tamanho: tamanho EXATO que o cliente pediu. Copie literal: se cliente disse "G",
                 passe "G" (NÃO "GG"). Se disse "M", passe "M". Se disse "42", passe "42".
                 Nunca expanda uma letra (G≠GG, M≠MM, P≠PP). Apenas colapse digitação
                 repetida em ≥4 (GGGG → GG). Use None só se o cliente realmente não falou
                 tamanho (ex: produtos sexshop, cosméticos).
    """
    print(f"\n[SEMÂNTICO] Buscando: '{termo_cliente}' | Tamanho recebido: {tamanho}")

    # 0. Normalização do Tamanho
    tamanho_alvo = tamanho.upper().strip() if tamanho else None

    # 0.1. DEFESA contra LLM drift (Risco B4): se cliente disse uma letra única
    # (ex: "G") e LLM passou outra (ex: "GG"), preferimos o que o cliente disse.
    # Heurística: comparar tamanho_alvo com tokens extraídos da última msg do user.
    if tamanho_alvo:
        ultimo_user = _user_last_msg.get(_consultar_estoque_active_user_id)
        tokens_user = _tamanhos_validos_na_msg(ultimo_user) if ultimo_user else []
        if tokens_user and tamanho_alvo not in tokens_user:
            # LLM passou tamanho que não bate com o que user disse na última msg.
            # Substitui pelo primeiro tamanho que o user mencionou.
            corrigido = tokens_user[0]
            print(f"[GUARD] LLM passou '{tamanho_alvo}' mas user disse {tokens_user}. Forçando '{corrigido}'.")
            tamanho_alvo = corrigido

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
            'filtro_tamanho': tamanho_alvo
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

    # 3. Filtragem por Tamanho (REDUNDÂNCIA / SEGURANÇA)
    # Mesmo com o banco filtrando, mantemos isso para garantir ou caso a RPC falhe silenciosamente no filtro
    validados = []

    for p in produtos_candidatos:
        # Se o cliente pediu tamanho, validamos se o produto bate
        if tamanho_alvo:
            if p['tamanho'].upper() == tamanho_alvo:
                validados.append(p)
        else:
            # Para cosméticos/acessórios, aceitamos o que vier com maior similaridade
            validados.append(p)

    if not validados:
        return {"status": "vazio", "msg": f"Não encontrei nada disponível no tamanho {tamanho_alvo}."}

    # 4. Top-K final preservando ordem do boost (set destruía ordenação)
    vistos = set()
    selecao = []
    for p in validados[:10]:
        chave = p.get('id_unico')
        if chave and chave not in vistos:
            vistos.add(chave)
            selecao.append(p)

    print(f"[SEMÂNTICO] Retornando {len(selecao)} itens.")
    return {"status": "sucesso", "produtos": selecao}

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
    try:
        resp = (
            supabase.table("produtos_estoque")
            .select("id_unico, id_produto, id_loja, loja, nome, tamanho, preco_varejo, preco_atacado, estoque, grupo_id, nome_grupo")
            .eq("id_produto", str(id_produto))
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

    Returns:
        {status, hoje_dia_semana, promocoes: [{nome, categoria, percentual, formas_pagamento, troca_permitida, observacao}]}
    """
    try:
        from datetime import datetime, timezone, timedelta
        # America/Sao_Paulo = UTC-3
        agora = datetime.now(timezone(timedelta(hours=-3)))
        # Postgres extract(dow): 0=domingo. Python weekday(): 0=segunda.
        # Vamos usar o padrão Postgres: 0=dom, 1=seg, ..., 6=sab.
        dow = (agora.weekday() + 1) % 7

        resp = (
            supabase.table("promocoes_ativas")
            .select("nome, categoria, percentual, formas_pagamento, troca_permitida, observacao")
            .eq("dia_semana", dow)
            .eq("ativa", True)
            .execute()
        )
        promos = resp.data or []
    except Exception as e:
        print(f"❌ Erro verificar_promocao_hoje: {e}")
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
                if len(resp_str) > 2000:
                    resp_str = resp_str[:2000] + "...(truncated)"
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
    Retorna (resposta_texto, produtos_recomendados, json_ok).
    Em caso de falha, retorna (texto_bruto, [], False) e o caller cai no fallback regex.
    """
    if not texto_bruto:
        return "", [], False
    texto = texto_bruto.strip()
    # Gemini pode envolver em ```json ... ``` se o response_schema for ignorado.
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(texto)
    except json.JSONDecodeError:
        return texto_bruto, [], False
    if not isinstance(obj, dict):
        return texto_bruto, [], False
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
    return str(resposta), ids_int, True


def renderizar_mensagem_estruturada(user_id, resposta_texto, ids_recomendados, cache):
    """
    Caminho determinístico: envia resposta_texto seguido de cards
    `*Nome* - R$ Valor` + imagem por id, usando dados canônicos do cache.
    Ids fora do cache do turno atual são ignorados (e logados).
    """
    if resposta_texto and resposta_texto.strip():
        enviar_mensagem_whatsapp(user_id, resposta_texto.strip())

    enviados = 0
    ignorados = []
    for pid in ids_recomendados[:5]:  # hard cap
        info = cache.get(int(pid))
        if not info:
            ignorados.append(pid)
            continue
        nome = info.get("nome") or f"Produto {pid}"
        preco_str = _formatar_preco(info.get("preco"))
        legenda = f"*{nome}* - {preco_str}"
        url = info.get("imagem")
        if url:
            time.sleep(1.0)
            enviar_midia_whatsapp(user_id, url, legenda)
        else:
            time.sleep(0.5)
            enviar_mensagem_whatsapp(user_id, legenda)
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

def process_and_respond(user_id):
    global _consultar_estoque_active_user_id
    buffer = message_buffers.get(user_id)
    if not buffer: return
    texto_completo = buffer['text']
    print(f"DEBUG INPUT: '{texto_completo}'")

    # Define contexto do turno usado por tools (defesa B4 em consultar_estoque)
    _user_last_msg[user_id] = texto_completo
    _consultar_estoque_active_user_id = user_id

    save_message(user_id, "user", texto_completo)

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
        )
        try:
            model = genai.GenerativeModel(generation_config=GENERATION_CONFIG, **modelo_args)
            json_mode_enabled = True
        except Exception as e_schema:
            print(f"⚠️ response_schema rejeitado pelo SDK ({e_schema}); modo livre.")
            model = genai.GenerativeModel(**modelo_args)
            json_mode_enabled = False

        history = get_history(user_id)
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        t_inicio = time.perf_counter()
        response = chat.send_message(texto_completo)
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
        resposta_limpa, ids_recomendados, json_ok = parsear_resposta_json(resposta_texto)

        fallback_usado = False
        if json_ok:
            print(f"✅ JSON parse ok | ids={ids_recomendados} | cache_size={len(cache_produtos)}")
            renderizar_mensagem_estruturada(user_id, resposta_limpa, ids_recomendados, cache_produtos)
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
        try:
            log_turn(
                user_id=user_id,
                user_input=texto_completo,
                tool_calls=[],
                final_output="",
                latency_ms=0,
                model_name='gemini-3-flash-preview',
                output_format="error",
                fallback_used=False,
                error=str(e),
            )
        except Exception:
            pass

    # Cleanup protegido pelo lock (evita race com novo webhook chegando no fim do turn)
    user_lock = _get_user_lock(user_id)
    with user_lock:
        message_buffers.pop(user_id, None)

def enviar_midia_whatsapp(numero, url_midia, legenda):
    """Envia mídia (imagem/pdf) via UazAPI."""
    # Deriva a URL de mídia baseada na URL de texto configurada no ENV
    # Ex: .../send/text -> .../send/media
    if UAZAPI_URL and "send/text" in UAZAPI_URL:
        url = UAZAPI_URL.replace("send/text", "send/media")
    else:
        # Fallback ou se a URL for diferente
        url = "https://vennx.uazapi.com/send/media"
    
    headers = {
        "token": UAZAPI_TOKEN, 
        "Content-Type": "application/json"
    }
    
    payload = {
        "number": numero,
        "type": "image", # Assumindo imagem por enquanto
        "file": url_midia,
        "docName": "foto_produto.jpg",
        "text": legenda # Legenda vai no campo text conforme documentação
    }
    
    try:
        print(f"📸 Enviando Mídia para {numero}: {url_midia}")
        response = requests.post(url, json=payload, headers=headers)
        print(f"Status Mídia: {response.status_code} | {response.text}")
    except Exception as e:
        print(f"Erro de conexão Uazapi Media: {e}")

def enviar_mensagem_whatsapp(numero, texto):
    # Se o texto contém tags de imagem, não deveria usar essa função, mas vamos manter como fallback
    headers = {"token": UAZAPI_TOKEN, "Content-Type": "application/json"}
    payload = {"number": numero, "text": texto}
    try:
        requests.post(UAZAPI_URL, json=payload, headers=headers)
    except Exception as e:
        print(f"Erro de conexão Uazapi: {e}")

@app.route('/webhook', methods=['POST'])
@app.route('/webhook/<evento>/<tipo>', methods=['POST'])
def webhook(evento=None, tipo=None):
    data = request.json
    msg_data = data.get('message', {})
    if msg_data.get('fromMe'): return jsonify({"status": "ignored"}), 200

    chat_id = msg_data.get('chatid')
    raw_text = msg_data.get('content')

    if not chat_id or not isinstance(raw_text, str):
        return jsonify({"status": "buffering"}), 200

    user_id = chat_id.split('@')[0]

    # Rate limit por user_id (ANTES de qualquer trabalho pesado)
    if not _check_rate_limit(user_id):
        print(f"[RATE_LIMIT] user={user_id} excedeu {RATE_LIMIT_MAX_MSGS} msgs/{RATE_LIMIT_WINDOW_SEC}s — descartando")
        return jsonify({"status": "rate_limited"}), 200

    # Lock por user_id evita race em escrita concorrente do buffer
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
        message_buffers[user_id]["timer"] = t
        t.start()

    return jsonify({"status": "buffering"}), 200

if __name__ == '__main__':
    # Inicia os schedulers de sincronização
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_otimizado, 'interval', minutes=30, misfire_grace_time=300)
    scheduler.add_job(sync_images, 'cron', hour=9, minute=0, misfire_grace_time=3600)
    scheduler.add_job(_job_purge_bot_turns, 'cron', hour=7, minute=0, misfire_grace_time=3600)
    scheduler.start()
    print("Schedulers iniciados: ERP 30min | Imagens diario 6h BRT | Purge bot_turns diario 4h BRT")

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)