import os
import requests
import logging
from supabase import create_client
import time
from dotenv import load_dotenv
import google.generativeai as genai

# Carrega o .env (necessário tanto standalone quanto via import)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_script_dir, "bot-control-panel", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

# --- Logging para o console (Railway) ---
import sys
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(message)s", datefmt="%H:%M:%S"))
log = logging.getLogger("sync_erp")
if not log.handlers:
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

# Configurações
GESTAO_CLICK_URL = os.getenv("GESTAO_CLICK_URL")
HEADERS = {
    "access-token": os.getenv("GESTAO_CLICK_TOKEN"),
    "secret-access-token": os.getenv("GESTAO_CLICK_SECRET"),
    "Content-Type": "application/json"
}
supabase_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Session HTTP reutilizável
session = requests.Session()
session.headers.update(HEADERS)

# Cache de embeddings em memória (por execução)
embedding_cache = {}


def get_embedding(text):
    if text in embedding_cache:
        return embedding_cache[text]
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=text,
            task_type="retrieval_document",
            output_dimensionality=768
        )
        embedding = result['embedding']
        embedding_cache[text] = embedding
        return embedding
    except Exception as e:
        log.warning(f"Embedding falhou: {text[:40]}... | {e}")
        return None


def get_lojas():
    """Busca todas as lojas disponíveis na API GestãoClick."""
    try:
        response = session.get("https://api.gestaoclick.com/lojas")
        if response.status_code != 200:
            log.error(f"Erro ao buscar lojas: Status {response.status_code}")
            return []
        data = response.json()
        lojas = data.get('data', [])
        log.info(f"🏪 {len(lojas)} lojas encontradas: {[l['nome'] for l in lojas]}")
        return lojas
    except Exception as e:
        log.error(f"Erro ao buscar lojas: {e}")
        return []


def carregar_estado_atual_do_banco():
    """Carrega todos os registros do banco: id_unico -> {nome, estoque, preco, id_loja}."""
    estado = {}
    offset = 0
    PAGE_SIZE = 1000

    while True:
        resp = supabase_client.table("produtos_estoque") \
            .select("id_unico, nome, estoque, preco, preco_varejo, preco_atacado, id_loja, grupo_id, nome_grupo") \
            .range(offset, offset + PAGE_SIZE - 1) \
            .execute()

        if not resp.data:
            break

        for row in resp.data:
            estado[row["id_unico"]] = {
                "nome": row["nome"],
                "estoque": float(row["estoque"] or 0),
                "preco": float(row["preco"] or 0),
                "preco_varejo": float(row.get("preco_varejo") or 0),
                "preco_atacado": float(row.get("preco_atacado") or 0),
                "id_loja": row.get("id_loja"),
                "grupo_id": row.get("grupo_id"),
                "nome_grupo": row.get("nome_grupo")
            }

        if len(resp.data) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return estado


def sync_otimizado():
    t_inicio = time.perf_counter()

    total_processados = 0
    total_upserts = 0
    total_embeddings_gerados = 0
    total_skipped = 0
    total_zerados = 0

    # 1) Busca lojas disponíveis
    lojas = get_lojas()
    if not lojas:
        log.error("Nenhuma loja encontrada. Abortando sync.")
        return

    # 2) Carrega estado do banco
    t0 = time.perf_counter()
    estado_banco = carregar_estado_atual_do_banco()
    t_banco = time.perf_counter() - t0

    ids_vindos_do_erp = set()
    t_api_total = 0
    t_embed_total = 0
    t_upsert_total = 0

    # 3) Loop por loja
    for loja in lojas:
        loja_id = loja['id']
        loja_nome = loja['nome']
        pagina = 1
        loja_processados = 0

        log.info(f"🏪 Processando loja: {loja_nome} (ID: {loja_id})")

        while True:
            params = {
                "loja": loja_id,
                "situacao": "2",
                "limite_por_pagina": 100,
                "pagina": pagina
            }

            try:
                t0 = time.perf_counter()
                response = session.get(GESTAO_CLICK_URL, params=params)
                t_api_total += time.perf_counter() - t0

                if response.status_code != 200:
                    log.error(f"API status {response.status_code} | Loja {loja_nome}")
                    break

                dados_erp = response.json().get('data', [])
                if not dados_erp:
                    break

                batch_upsert = []

                for produto in dados_erp:
                    p_id = produto['id']
                    base_nome = produto['nome']
                    variacoes = produto.get('variacoes', [])
                    grupo_id = produto.get('grupo_id')
                    nome_grupo = produto.get('nome_grupo')

                    # Extrai precos do produto base
                    prod_preco_varejo = 0.0
                    prod_preco_atacado = 0.0
                    prod_preco_base = float(produto.get('valor_venda') or 0)

                    valores_prod = produto.get('valores', [])
                    for v in valores_prod:
                        if v.get('nome_tipo') == 'Varejo':
                            prod_preco_varejo = float(v.get('valor_venda') or 0)
                        elif v.get('nome_tipo') == 'Atacado':
                            prod_preco_atacado = float(v.get('valor_venda') or 0)

                    if not prod_preco_varejo: prod_preco_varejo = prod_preco_base
                    if not prod_preco_atacado: prod_preco_atacado = prod_preco_varejo

                    registros_produto = []

                    if variacoes and isinstance(variacoes, list):
                        for item in variacoes:
                            variacao = item.get('variacao', {})
                            tamanho = str(variacao.get('nome') or 'ÚNICO').strip().upper()
                            if not tamanho: tamanho = 'ÚNICO'

                            qtd = float(variacao.get('estoque') or 0)

                            var_preco_varejo = 0.0
                            var_preco_atacado = 0.0
                            var_preco_base = float(variacao.get('valor_venda') or 0)

                            valores_var = variacao.get('valores', [])
                            if valores_var:
                                for v in valores_var:
                                    if v.get('nome_tipo') == 'Varejo':
                                        var_preco_varejo = float(v.get('valor_venda') or 0)
                                    elif v.get('nome_tipo') == 'Atacado':
                                        var_preco_atacado = float(v.get('valor_venda') or 0)
                            else:
                                var_preco_varejo = var_preco_base
                                var_preco_atacado = var_preco_base

                            final_varejo = var_preco_varejo if var_preco_varejo > 0 else prod_preco_varejo
                            final_atacado = var_preco_atacado if var_preco_atacado > 0 else prod_preco_atacado

                            if qtd > 0:
                                registros_produto.append({
                                    "id_unico": f"{loja_id}_{p_id}_{tamanho}",
                                    "id_produto": p_id,
                                    "id_loja": loja_id,
                                    "loja": loja_nome,
                                    "nome": base_nome,
                                    "tamanho": tamanho,
                                    "preco": final_varejo,
                                    "preco_varejo": final_varejo,
                                    "preco_atacado": final_atacado,
                                    "estoque": qtd,
                                    "grupo_id": grupo_id,
                                    "nome_grupo": nome_grupo
                                })
                    else:
                        qtd = float(produto.get('estoque', 0))

                        if qtd > 0:
                            registros_produto.append({
                                "id_unico": f"{loja_id}_{p_id}_UNICO",
                                "id_produto": p_id,
                                "id_loja": loja_id,
                                "loja": loja_nome,
                                "nome": base_nome,
                                "tamanho": "ÚNICO",
                                "preco": prod_preco_varejo,
                                "preco_varejo": prod_preco_varejo,
                                "preco_atacado": prod_preco_atacado,
                                "estoque": qtd,
                                "grupo_id": grupo_id,
                                "nome_grupo": nome_grupo
                            })

                    for reg in registros_produto:
                        id_unico = reg["id_unico"]
                        ids_vindos_do_erp.add(id_unico)

                        existente = estado_banco.get(id_unico)

                        if existente is None:
                            t0 = time.perf_counter()
                            vetor = get_embedding(base_nome)
                            t_embed_total += time.perf_counter() - t0
                            if vetor:
                                reg["embedding"] = vetor
                            total_embeddings_gerados += 1
                            batch_upsert.append(reg)

                        elif existente["nome"] != base_nome:
                            t0 = time.perf_counter()
                            vetor = get_embedding(base_nome)
                            t_embed_total += time.perf_counter() - t0
                            if vetor:
                                reg["embedding"] = vetor
                            total_embeddings_gerados += 1
                            batch_upsert.append(reg)

                        elif (existente["estoque"] != reg["estoque"] or
                              existente["preco"] != reg["preco"] or
                              existente["preco_varejo"] != reg["preco_varejo"] or
                              existente["preco_atacado"] != reg["preco_atacado"] or
                              existente.get("grupo_id") != reg["grupo_id"] or
                              existente.get("nome_grupo") != reg["nome_grupo"]):
                            batch_upsert.append(reg)

                        else:
                            total_skipped += 1

                    total_processados += 1
                    loja_processados += 1

                # Upsert em lote
                if batch_upsert:
                    t0 = time.perf_counter()
                    supabase_client.table("produtos_estoque").upsert(batch_upsert).execute()
                    t_upsert_total += time.perf_counter() - t0
                    total_upserts += len(batch_upsert)

                pagina += 1

            except Exception as e:
                log.error(f"Erro página {pagina} loja {loja_nome}: {e}")
                break

        log.info(f"✅ Loja {loja_nome}: {loja_processados} produtos processados")

    # 4) Zera estoque dos que saíram
    ids_para_zerar = [
        id_u for id_u in (set(estado_banco.keys()) - ids_vindos_do_erp)
        if estado_banco[id_u]["estoque"] > 0
    ]

    if ids_para_zerar:
        t0 = time.perf_counter()
        for i in range(0, len(ids_para_zerar), 200):
            lote = ids_para_zerar[i:i + 200]
            supabase_client.table("produtos_estoque").upsert(
                [{"id_unico": id_u, "estoque": 0} for id_u in lote]
            ).execute()
        t_upsert_total += time.perf_counter() - t0
        total_zerados = len(ids_para_zerar)

    # 5) Log final com tempos
    t_total = time.perf_counter() - t_inicio
    log.info(
        f"SYNC OK | {t_total:.1f}s total | "
        f"lojas:{len(lojas)} banco:{t_banco:.1f}s api:{t_api_total:.1f}s embed:{t_embed_total:.1f}s upsert:{t_upsert_total:.1f}s | "
        f"processados:{total_processados} upserts:{total_upserts} "
        f"embeds_novos:{total_embeddings_gerados} skipped:{total_skipped} zerados:{total_zerados}"
    )


if __name__ == "__main__":
    sync_otimizado()