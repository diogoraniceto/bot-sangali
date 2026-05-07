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

def get_history(user_id, limit=10):
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

# ================= NOVA FERRAMENTA DE BUSCA (SUPABASE) =================

def consultar_estoque_supabase(termo_cliente: str, tamanho: str = None):
    """
    Realiza busca semântica no Supabase usando Embeddings.
    Filtra por tamanho e realiza curadoria de preços.
    """
    print(f"\n[SEMÂNTICO] Buscando: '{termo_cliente}' | Tamanho: {tamanho}")
    
    # 0. Normalização do Tamanho (Agora feita no início para enviar ao banco)
    tamanho_alvo = tamanho.upper().strip() if tamanho else None

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
            .eq("id_produto", id_produto)
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
            .select("id_produto, nome, preco_varejo, preco_atacado")
            .in_("id_produto", ids)
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
        pa = info["preco_atacado"] or pv * (1 - cfg["desconto_avista"])
        # à prazo derivado de varejo com desconto separado:
        pap = pv * (1 - cfg["desconto_aprazo"])

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


def calcular_frete_estimado(cep_destino: str, id_produto: int = None, quantidade: int = 1, nome_produto: str = None):
    """
    Estima o custo de frete da Sangali até o CEP do cliente.

    Use esta ferramenta quando o cliente perguntar sobre frete, entrega, prazo ou custo de envio.
    IMPORTANTE: Chame esta ferramenta SOMENTE após o cliente informar o CEP.

    Forneça preferencialmente o `id_produto` (mais preciso). `nome_produto`
    só deve ser usado se você não souber o id ainda.

    Args:
        cep_destino:  CEP do cliente (ex: "29900-161" ou "29900161").
        id_produto:   id numérico do produto principal (do consultar_estoque_supabase).
                      Determinístico — peso/dimensão lidos do banco.
        quantidade:   Quantidade de itens estimados (padrão 1, máximo 50).
        nome_produto: Fallback textual quando id_produto desconhecido.
                      Usado para classificar peso por keyword.
    """
    cep_limpo = _validar_cep(cep_destino)
    if not cep_limpo:
        return {"status": "erro_cep", "msg": "CEP inválido. Informe o CEP com 8 dígitos, ex: 87013-000."}

    try:
        quantidade = max(1, min(50, int(quantidade)))
    except (TypeError, ValueError):
        quantidade = 1

    nome_para_peso = None
    if id_produto:
        try:
            r = supabase.table("produtos_estoque").select("nome, nome_grupo").eq("id_produto", int(id_produto)).limit(1).execute()
            if r.data:
                nome_para_peso = (r.data[0].get("nome") or "") + " " + (r.data[0].get("nome_grupo") or "")
        except Exception as e:
            print(f"⚠️ Frete: lookup id_produto={id_produto} falhou: {e}")

    if not nome_para_peso:
        nome_para_peso = nome_produto or ""

    if not nome_para_peso:
        return {"status": "erro", "msg": "Informe id_produto ou nome_produto para estimar o pacote."}

    try:
        cfg = supabase.table("bot_settings").select("cep_origem").eq("id", 1).single().execute()
        cep_origem = _validar_cep((cfg.data or {}).get("cep_origem", ""))
    except Exception as e:
        print(f"⚠️ Frete: erro ao buscar cep_origem: {e}")
        cep_origem = None

    if not cep_origem:
        return {"status": "erro_config", "msg": "Configuração da loja ausente. Transfira para atendente."}

    peso_g, alt, larg, comp = _estimar_pacote(nome_para_peso, quantidade)
    print(f"📦 Frete: cep_destino={cep_limpo} | id={id_produto} nome='{nome_para_peso[:40]}' qtd={quantidade} | pacote={peso_g}g {alt}x{larg}x{comp}cm")

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

# O modelo será inicializado dinamicamente dentro da função process_and_respond
# model = genai.GenerativeModel(...)

# ================= LÓGICA DA CONVERSA & WEBHOOK =================

def process_and_respond(user_id):
    buffer = message_buffers.get(user_id)
    if not buffer: return
    texto_completo = buffer['text']
    print(f"DEBUG INPUT: '{texto_completo}'")
    
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
        model = genai.GenerativeModel(
            model_name='gemini-3-flash-preview',
            tools=[
                consultar_estoque_supabase,
                consultar_produto_por_id,
                calcular_total,
                verificar_promocao_hoje,
                transferir_para_atendente,
                calcular_frete_estimado,
            ],
            system_instruction=system_instruction_dinamica
        )

        history = get_history(user_id)
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        response = chat.send_message(texto_completo)
        resposta_texto = response.text
        print(f"DEBUG OUTPUT: '{resposta_texto}'")

        save_message(user_id, "model", resposta_texto)
        
        # NOVA LÓGICA: Split para separar textos e imagens de forma linear e robusta.
        # O padrão captura a URL no grupo (parenteses), fazendo com que o split retorne:
        # [Texto1, URL1, Texto2, URL2, Texto3...]
        parts = re.split(r"\[IMAGEM:(.*?)\]", resposta_texto, flags=re.DOTALL)
        
        print(f"DEBUG SPLIT: Encontradas {len(parts)} partes na resposta.")

        if len(parts) > 1:
            # Iteramos de 2 em 2: (Texto anterior, URL da imagem)
            # O último elemento sobra (texto após a última imagem)
            for i in range(0, len(parts) - 1, 2):
                texto_legenda = parts[i].strip()
                url_imagem = parts[i+1].strip()
                
                if url_imagem:
                    print(f"📸 Enviando Imagem {i//2 + 1}: Legenda='{texto_legenda[:30]}...' | Url='{url_imagem}'")
                    enviar_midia_whatsapp(user_id, url_imagem, texto_legenda)
                    time.sleep(1.5) # Delay essencial para garantir a ordem no WhatsApp
            
            # Verifica se sobrou texto após a última imagem
            ultimo_texto = parts[-1].strip()
            if ultimo_texto:
                print(f"💬 Enviando texto final: '{ultimo_texto[:30]}...'")
                enviar_mensagem_whatsapp(user_id, ultimo_texto)
        else:
            # Caso sem imagens, envia texto normal
            print("DEBUG: Nenhuma tag de imagem encontrada. Enviando texto único.")
            enviar_mensagem_whatsapp(user_id, resposta_texto)

    except Exception as e:
        print(f"Erro IA: {e}")
    
    del message_buffers[user_id]

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

    if chat_id and isinstance(raw_text, str):
        user_id = chat_id.split('@')[0]
        if user_id not in message_buffers:
            message_buffers[user_id] = {"text": raw_text, "timer": None}
        else:
            message_buffers[user_id]["text"] += f" {raw_text}"
            if message_buffers[user_id]["timer"]: message_buffers[user_id]["timer"].cancel()

        t = threading.Timer(10.0, process_and_respond, args=[user_id])
        message_buffers[user_id]["timer"] = t
        t.start()

    return jsonify({"status": "buffering"}), 200

if __name__ == '__main__':
    # Inicia os schedulers de sincronização
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_otimizado, 'interval', minutes=30, misfire_grace_time=300)
    scheduler.add_job(sync_images, 'cron', hour=9, minute=0, misfire_grace_time=3600)
    scheduler.start()
    print("⏰ Schedulers iniciados: ERP (30 min) | Imagens (diário 6h BRT)")

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)