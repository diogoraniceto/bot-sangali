"""Re-embed em massa de produtos_estoque com gemini-embedding-001 (768 dims).

Usa a convencao CANONICA de `embedding_text.py`: o texto embedado e SOMENTE o
nome do produto. Antes desta frente este script embedava "<nome> | Tamanho: <t>",
divergindo do sync_erp.py — 0,876 de cosseno contra o canonico, num ranking cujo
top-12 cabe em 0,018 de spread. Era isso que deixava a base com convencao mista.

USO
    python regenerar_embeddings.py --dry-run          # so CONTA o que mudaria
    python regenerar_embeddings.py                    # re-embed TOTAL (canonico)
    python regenerar_embeddings.py --only-missing     # so linhas com embedding NULL
    python regenerar_embeddings.py --skip=820         # retoma de onde parou
    python regenerar_embeddings.py --limit=3          # teste de caminho (poucas linhas)

FLAGS
    --dry-run       nao escreve nada: imprime quantos pares (id_produto, nome),
                    quantas chamadas de API e quantas linhas seriam tocadas.
    --only-missing  filtra `embedding IS NULL`. So use DEPOIS de conferir que
                    este arquivo ja usa texto_embedding_produto (veja abaixo).
    --skip=N        pula os N primeiros pares da lista. A lista e deterministica
                    (order by id_produto, nome), entao --skip retoma exatamente
                    de onde a execucao anterior morreu.
    --limit=N       processa no maximo N pares depois do skip.

RESTRICOES DURAS (nao "simplifique" isto)
  * A escrita e `update` com dict UNICO, filtrada por (id_produto, nome).
    NUNCA troque por upsert em lote: o postgrest-py monta
    ?columns=<UNIAO das chaves do lote> e um lote heterogeneo grava NULL nas
    colunas que faltam — foi assim que 1.203 vetores foram apagados (ver
    sync_erp.lotes_homogeneos e tests/test_sync_upsert_homogeneo.py).
  * A chave de escrita e (id_produto, nome), nao id_produto: 24 id_produto tem
    mais de um `nome` distinto (280 linhas, 187 com estoque). Deduplicar so por
    id_produto estamparia o vetor de um nome arbitrario nas linhas do outro nome
    — exatamente a classe de defeito que esta frente mata. Custo: 24 embeddings
    extras.
  * NUNCA rodar este script com --only-missing numa versao que ainda monte o
    texto com "| Tamanho:" — sobrescreveria linhas com vetor BOM por vetor de
    outra convencao.

DEPOIS de um re-embed total, rodar no banco:
    REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx;
    ANALYZE produtos_estoque;
HNSW nao reaproveita entradas mortas; reescrever ~6.7k vetores deixa metade do
grafo morto e degrada recall/latencia no caminho que a RPC usa.
"""

import os
import sys
import time
from dotenv import load_dotenv
import google.generativeai as genai
from supabase import create_client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from embedding_text import MODELO_EMBEDDING, DIM_EMBEDDING, texto_embedding_produto

# O console do Windows abre em cp1252 e os emojis do log estouram UnicodeEncodeError
# quando a saida e redirecionada (o dono roda este script no Windows, e o log de
# ~1.576 linhas e a unica forma de saber onde retomar com --skip).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Carrega .env da pasta atual ou da subpasta bot-control-panel
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot-control-panel", ".env")
load_dotenv(env_path)
print(f"📂 Carregando .env de: {env_path}")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

genai.configure(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Cache por TEXTO: nomes repetidos entre id_produto diferentes nao pagam 2 chamadas.
_cache_embedding = {}


def gerar_embedding(texto):
    """Gera embedding do texto canonico. Retorna None em falha (o chamador conta o erro)."""
    if texto in _cache_embedding:
        return _cache_embedding[texto]
    try:
        result = genai.embed_content(
            model=MODELO_EMBEDDING,
            content=texto,
            task_type="retrieval_document",
            output_dimensionality=DIM_EMBEDDING,
            # timeout OBRIGATORIO: sem ele uma chamada travada pendura o script
            # inteiro no meio de um re-embed de ~40 min.
            request_options={"timeout": 30},
        )
        emb = result['embedding']
        _cache_embedding[texto] = emb
        return emb
    except Exception as e:
        print(f"  ❌ Erro: {e}")
        return None


def _arg_int(prefixo, default=0):
    for a in sys.argv[1:]:
        if a.startswith(prefixo):
            try:
                return int(a.split("=", 1)[1])
            except (IndexError, ValueError):
                print(f"[!] valor invalido em '{a}', usando {default}")
    return default


def carregar_pares(only_missing):
    """Todos os pares (id_produto, nome) distintos, em ordem DETERMINISTICA.

    A ordem (id_produto, nome) e o que faz --skip=N retomar no ponto exato.
    Sem `.order()` o `.range()` do PostgREST nao garante ordem entre paginas e a
    retomada pularia/repetiria linhas.
    """
    linhas = []
    page_size = 1000
    offset = 0
    while True:
        q = supabase.table("produtos_estoque").select("id_produto, nome")
        if only_missing:
            q = q.is_("embedding", "null")
        resp = q.order("id_produto").order("nome").range(offset, offset + page_size - 1).execute()
        batch = resp.data or []
        if not batch:
            break
        linhas.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    # Dedupe por (id_produto, nome) — NAO por id_produto (ver RESTRICOES DURAS).
    vistos = set()
    pares = []
    for row in linhas:
        chave = (row.get("id_produto"), row.get("nome"))
        if chave in vistos:
            continue
        vistos.add(chave)
        pares.append(chave)
    return len(linhas), pares


def main():
    only_missing = "--only-missing" in sys.argv
    dry_run = "--dry-run" in sys.argv
    skip = _arg_int("--skip=", 0)
    limite = _arg_int("--limit=", 0)

    print(f"[i] modo: {'somente sem embedding' if only_missing else 'TODOS os produtos'}"
          f"{' | DRY-RUN (nao escreve)' if dry_run else ''}")

    n_linhas, pares = carregar_pares(only_missing)
    total_pares = len(pares)
    if skip:
        pares = pares[skip:]
    if limite:
        pares = pares[:limite]

    textos = [texto_embedding_produto(nome) for _, nome in pares]
    n_vazios = sum(1 for t in textos if not t)
    n_chamadas = len({t for t in textos if t})

    print(f"[i] linhas lidas: {n_linhas} | pares (id_produto, nome) distintos: {total_pares}")
    print(f"[i] a processar nesta execucao: {len(pares)} (skip={skip} limit={limite or '-'})")
    print(f"[i] chamadas de API estimadas (textos distintos): {n_chamadas} | pares sem nome (skip): {n_vazios}")
    print("=" * 60)

    if dry_run:
        print("[dry-run] nada foi escrito. Remova --dry-run para executar.")
        return

    atualizados = 0
    erros = 0
    pulados = 0

    for i, (id_produto, nome) in enumerate(pares):
        texto = texto_embedding_produto(nome)
        if not texto:
            # Sem nome nao existe texto canonico: embedar "" gravaria um vetor que
            # nao descreve nada e ainda concorreria no ranking de similaridade.
            pulados += 1
            print(f"[{skip+i+1}/{total_pares}] id={id_produto} SEM NOME — pulado")
            continue

        print(f"[{skip+i+1}/{total_pares}] {texto[:50]}...", end=" ")
        embedding = gerar_embedding(texto)

        if embedding:
            # update com dict UNICO, filtrado por (id_produto, nome). Ver
            # RESTRICOES DURAS no topo: nunca upsert em lote aqui.
            supabase.table("produtos_estoque").update({
                "embedding": embedding
            }).eq("id_produto", id_produto).eq("nome", nome).execute()
            atualizados += 1
            print("✅")
        else:
            erros += 1
            print("❌")

        # Pausa para nao estourar rate limit da API (1500 RPM)
        if (i + 1) % 100 == 0:
            print(f"\n⏸️  Pausa de 5s para evitar rate limit... ({skip+i+1}/{total_pares})\n")
            time.sleep(5)

    print("=" * 60)
    print(f"✅ Atualizados: {atualizados}")
    print(f"❌ Erros: {erros}")
    print(f"⏭️  Pulados (sem nome): {pulados}")
    print(f"📦 Pares processados: {len(pares)} de {total_pares}")
    if erros:
        print(f"[!] Houve erro. Para retomar do ponto: --skip={skip + len(pares)}")
    print("🎉 Pronto. Rode agora: REINDEX INDEX CONCURRENTLY produtos_estoque_embedding_idx; ANALYZE produtos_estoque;")


if __name__ == "__main__":
    main()
