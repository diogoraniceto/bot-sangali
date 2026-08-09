import os
import requests
import logging
from collections import defaultdict
from supabase import create_client
import time
from datetime import datetime, timezone
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

# Timeout obrigatorio em toda chamada HTTP: sem isso, um request travado congela
# a thread do scheduler e o sync para de rodar (foi a causa do estoque parar em 03/07).
REQUEST_TIMEOUT = int(os.getenv("SYNC_HTTP_TIMEOUT", "30"))

# Cache de embeddings em memória (por execução)
embedding_cache = {}

# Heartbeat do sync: atualizado a cada execucao concluida (mesmo com 0 mudancas).
# E o sinal real de "o sync rodou" — diferente de last_sync na tabela, que so muda
# quando um produto muda. Lido pelo /health e pelo watchdog de alerta (bot.py).
LAST_RUN_AT = None    # datetime UTC da ultima execucao
LAST_RUN_OK = None    # bool: ultima execucao terminou sem abortar
LAST_RUN_INFO = ""    # resumo curto (contadores)


def get_embedding(text):
    if text in embedding_cache:
        return embedding_cache[text]
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=text,
            task_type="retrieval_document",
            output_dimensionality=768,
            request_options={"timeout": 30}
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
        response = session.get("https://api.gestaoclick.com/lojas", timeout=REQUEST_TIMEOUT)
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


def _strip_acentos_upper(s):
    """Normaliza string para comparação: uppercase + strip de acentos comuns."""
    if not s:
        return ""
    t = s.upper().strip()
    repl = (('Á','A'),('À','A'),('Â','A'),('Ã','A'),('Ä','A'),
            ('É','E'),('È','E'),('Ê','E'),('Ë','E'),
            ('Í','I'),('Ì','I'),('Î','I'),('Ï','I'),
            ('Ó','O'),('Ò','O'),('Ô','O'),('Õ','O'),('Ö','O'),
            ('Ú','U'),('Ù','U'),('Û','U'),('Ü','U'),
            ('Ç','C'),('Ñ','N'))
    for src, dst in repl:
        t = t.replace(src, dst)
    return t


def _classificar_tipo_preco(nome_tipo):
    """Classifica nome_tipo cru da API GestãoClick em chave canônica.

    A API retorna 4 tipos por produto:
        VAREJO CRÉDITO   -> 'varejo'           (preço de tabela / crédito)
        VAREJO A VISTA   -> 'varejo_avista'    (varejo com desconto à vista)
        ATACADO A VISTA  -> 'atacado_avista'   (atacado à vista, melhor preço)
        ATACADO CRÉDITO  -> 'atacado_aprazo'   (atacado parcelado)

    Retorna None se nome_tipo não casa com nenhum padrão conhecido.
    """
    t = _strip_acentos_upper(nome_tipo)
    if not t:
        return None
    tem_varejo = 'VAREJO' in t
    tem_atacado = 'ATACADO' in t
    tem_vista = 'VISTA' in t
    if tem_varejo and not tem_atacado:
        return 'varejo_avista' if tem_vista else 'varejo'
    if tem_atacado and not tem_varejo:
        return 'atacado_avista' if tem_vista else 'atacado_aprazo'
    return None


def _extrair_precos(valores_list, valor_venda_base):
    """Dado o array `valores` (do produto ou da variação) e o valor_venda base,
    retorna dict com as 4 chaves de preço, com fallbacks coerentes.
    """
    out = {'varejo': 0.0, 'varejo_avista': 0.0, 'atacado_avista': 0.0, 'atacado_aprazo': 0.0}
    for v in (valores_list or []):
        chave = _classificar_tipo_preco(v.get('nome_tipo'))
        if chave is None:
            continue
        try:
            out[chave] = float(v.get('valor_venda') or 0)
        except (TypeError, ValueError):
            pass
    base = float(valor_venda_base or 0)
    if not out['varejo']:
        out['varejo'] = base
    if not out['varejo_avista']:
        out['varejo_avista'] = out['varejo']
    if not out['atacado_avista']:
        out['atacado_avista'] = out['varejo']
    if not out['atacado_aprazo']:
        out['atacado_aprazo'] = out['atacado_avista']
    return out


def lotes_homogeneos(batch):
    """Quebra um lote de registros em sub-lotes com EXATAMENTE o mesmo conjunto de chaves.

    O postgrest-py monta `?columns=<UNIAO das chaves do lote>` num upsert de lista.
    Num lote heterogeneo, o registro que NAO traz 'embedding' e gravado com NULL
    nessa coluna (o default da coluna e NULL, logo `Prefer: missing=default`
    tambem nao salva o valor antigo — ja testado). Foi assim que 1.203 vetores
    foram apagados. Agrupar por conjunto de chaves resolve a CLASSE do bug.
    NUNCA volte a fazer um upsert unico com shapes diferentes.

    Ordem: os sub-lotes saem na ordem da PRIMEIRA aparicao de cada shape, e dentro
    de cada sub-lote a ordem original e preservada — o mesmo id_unico repetido no
    mesmo shape continua sendo aplicado na ordem em que entrou.
    """
    grupos = defaultdict(list)
    for _reg in (batch or []):
        grupos[frozenset(_reg.keys())].append(_reg)
    return list(grupos.values())


def carregar_estado_atual_do_banco():
    """Carrega todos os registros do banco: id_unico -> {nome, estoque, preço, id_loja}."""
    estado = {}
    offset = 0
    PAGE_SIZE = 1000

    while True:
        # .order("id_unico") e OBRIGATORIO: sem ORDER BY explicito o Postgres nao
        # garante a mesma ordem entre as paginas do .range(), e uma linha pulada
        # some do `estado`. Linha ausente do estado e tratada como registro NOVO
        # (ramo `existente is None`), leva get_embedding() e entra no lote COM a
        # chave 'embedding' — ou seja, a paginacao instavel FABRICA o portador que
        # apagava o vetor das outras linhas da mesma pagina.
        resp = supabase_client.table("produtos_estoque") \
            .select("id_unico, nome, estoque, preco, preco_varejo, preco_varejo_avista, preco_atacado, preco_atacado_aprazo, id_loja, grupo_id, nome_grupo") \
            .order("id_unico") \
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
                "preco_varejo_avista": float(row.get("preco_varejo_avista") or 0),
                "preco_atacado": float(row.get("preco_atacado") or 0),
                "preco_atacado_aprazo": float(row.get("preco_atacado_aprazo") or 0),
                "id_loja": row.get("id_loja"),
                "grupo_id": row.get("grupo_id"),
                "nome_grupo": row.get("nome_grupo")
            }

        if len(resp.data) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return estado


def sync_otimizado():
    global LAST_RUN_AT, LAST_RUN_OK, LAST_RUN_INFO
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
        LAST_RUN_AT = datetime.now(timezone.utc); LAST_RUN_OK = False; LAST_RUN_INFO = "abortou: sem lojas do ERP"
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
                response = session.get(GESTAO_CLICK_URL, params=params, timeout=REQUEST_TIMEOUT)
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

                    # Extrai os 4 preços do produto base
                    precos_prod = _extrair_precos(
                        produto.get('valores', []),
                        produto.get('valor_venda')
                    )

                    registros_produto = []

                    if variacoes and isinstance(variacoes, list):
                        for item in variacoes:
                            variacao = item.get('variacao', {})
                            tamanho = str(variacao.get('nome') or 'ÚNICO').strip().upper()
                            if not tamanho: tamanho = 'ÚNICO'

                            qtd = float(variacao.get('estoque') or 0)

                            # Variação tem seus próprios valores; senão herda do produto base
                            valores_var = variacao.get('valores') or []
                            base_var = variacao.get('valor_venda') or produto.get('valor_venda')
                            if valores_var:
                                precos_var = _extrair_precos(valores_var, base_var)
                            else:
                                precos_var = dict(precos_prod)

                            final = {
                                k: (precos_var[k] if precos_var[k] > 0 else precos_prod[k])
                                for k in ('varejo','varejo_avista','atacado_avista','atacado_aprazo')
                            }

                            if qtd > 0:
                                registros_produto.append({
                                    "id_unico": f"{loja_id}_{p_id}_{tamanho}",
                                    "id_produto": p_id,
                                    "id_loja": loja_id,
                                    "loja": loja_nome,
                                    "nome": base_nome,
                                    "tamanho": tamanho,
                                    "preco": final['varejo'],
                                    "preco_varejo": final['varejo'],
                                    "preco_varejo_avista": final['varejo_avista'],
                                    "preco_atacado": final['atacado_avista'],
                                    "preco_atacado_aprazo": final['atacado_aprazo'],
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
                                "preco": precos_prod['varejo'],
                                "preco_varejo": precos_prod['varejo'],
                                "preco_varejo_avista": precos_prod['varejo_avista'],
                                "preco_atacado": precos_prod['atacado_avista'],
                                "preco_atacado_aprazo": precos_prod['atacado_aprazo'],
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
                              existente.get("preco_varejo_avista", 0) != reg["preco_varejo_avista"] or
                              existente["preco_atacado"] != reg["preco_atacado"] or
                              existente.get("preco_atacado_aprazo", 0) != reg["preco_atacado_aprazo"] or
                              existente.get("grupo_id") != reg["grupo_id"] or
                              existente.get("nome_grupo") != reg["nome_grupo"]):
                            batch_upsert.append(reg)

                        else:
                            total_skipped += 1

                    total_processados += 1
                    loja_processados += 1

                # Upsert em lote — SEMPRE por shape homogeneo (ver lotes_homogeneos).
                # Os dois dict literais de registro acima tem chaves identicas; a
                # unica condicional e 'embedding', logo aqui saem 1 ou 2 sub-lotes.
                if batch_upsert:
                    t0 = time.perf_counter()
                    for _lote in lotes_homogeneos(batch_upsert):
                        supabase_client.table("produtos_estoque").upsert(_lote).execute()
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
    LAST_RUN_AT = datetime.now(timezone.utc); LAST_RUN_OK = True
    LAST_RUN_INFO = f"upserts:{total_upserts} skipped:{total_skipped} zerados:{total_zerados}"
    log.info(
        f"SYNC OK | {t_total:.1f}s total | "
        f"lojas:{len(lojas)} banco:{t_banco:.1f}s api:{t_api_total:.1f}s embed:{t_embed_total:.1f}s upsert:{t_upsert_total:.1f}s | "
        f"processados:{total_processados} upserts:{total_upserts} "
        f"embeds_novos:{total_embeddings_gerados} skipped:{total_skipped} zerados:{total_zerados}"
    )


if __name__ == "__main__":
    sync_otimizado()