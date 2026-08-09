"""Controle positivo pre/pos-fix do upsert de lote HETEROGENEO (F4).

O que prova, contra o Supabase REAL (tabela de teste, nunca produtos_estoque):
  Teste 1  lote heterogeneo (um registro COM 'embedding', outro SEM) APAGA o
           vetor de quem nao trouxe a chave. Se este teste NAO reproduzir, a
           versao instalada de postgrest-py mudou de comportamento e o pin de
           requirements.txt esta errado — pare e investigue antes de confiar
           no fix.
  Teste 2  o mesmo lote, quebrado por sync_erp.lotes_homogeneos(), PRESERVA os
           dois vetores e ainda aplica as demais colunas.
  Teste 3  propriedades puras de lotes_homogeneos() (sem rede).

NAO faz DDL. A tabela de apoio e criada UMA VEZ pela Management API (a chave
anon nao tem CREATE em public e o supabase-py nao executa SQL cru):

    create table if not exists public._test_emb_upsert (
      id_unico text primary key, nome text, estoque numeric, embedding vector(768));
    alter table public._test_emb_upsert disable row level security;
    grant select, insert, update, delete on public._test_emb_upsert
      to anon, authenticated, service_role;
    notify pgrst, 'reload schema';

E derrubada com `drop table if exists public._test_emb_upsert;` quando a frente
fecha (P4.20). Rodar:

    python tests/test_sync_upsert_homogeneo.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["ENABLE_INPROCESS_SYNC"] = "0"       # nao sobe scheduler/watchdog
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import sync_erp

TABELA = "_test_emb_upsert"
sb = sync_erp.supabase_client

falhas = []


def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


def vec(seed):
    """Vetor deterministico de 768 dims (mesma dimensao de produtos_estoque)."""
    return [((seed * 7 + i) % 100) / 100.0 for i in range(768)]


def _as_list(v):
    """PostgREST pode devolver vector como str '[0.1,0.2,...]' ou como list."""
    if v is None:
        return None
    if isinstance(v, str):
        return [float(x) for x in v.strip("[]").split(",") if x.strip() != ""]
    return [float(x) for x in v]


def ler():
    r = sb.table(TABELA).select("id_unico, nome, estoque, embedding").order("id_unico").execute()
    return {x["id_unico"]: x for x in (r.data or [])}


def limpar():
    sb.table(TABELA).delete().neq("id_unico", "___nunca___").execute()


def semear():
    """Estado inicial: A e B com vetor. Upsert homogeneo de proposito."""
    limpar()
    sb.table(TABELA).upsert([
        {"id_unico": "A", "nome": "PROD A", "estoque": 1, "embedding": vec(1)},
        {"id_unico": "B", "nome": "PROD B", "estoque": 1, "embedding": vec(2)},
    ]).execute()


try:
    try:
        sb.table(TABELA).select("id_unico").limit(1).execute()
    except Exception as e:
        print(f"[X] tabela de apoio '{TABELA}' inacessivel: {e}")
        print("    Recrie com o DDL do docstring deste arquivo (Management API) e rode de novo.")
        sys.exit(2)

    print("### 0. Estado inicial (upsert homogeneo: A e B com vetor)")
    semear()
    est = ler()
    ok(_as_list(est["A"]["embedding"]) == vec(1), "A semeado com vetor")
    ok(_as_list(est["B"]["embedding"]) == vec(2), "B semeado com vetor")

    print("\n### 1. PRE-FIX: lote HETEROGENEO apaga o vetor de quem nao traz a chave")
    # A = 'produto novo/renomeado' (portador: traz 'embedding').
    # B = 'so mudou estoque' (vitima: NAO traz 'embedding').
    lote_misto = [
        {"id_unico": "A", "nome": "PROD A", "estoque": 2, "embedding": vec(3)},
        {"id_unico": "B", "nome": "PROD B", "estoque": 9},
    ]
    sb.table(TABELA).upsert(lote_misto).execute()
    est = ler()
    ok(_as_list(est["A"]["embedding"]) == vec(3), "portador A gravou o vetor novo")
    ok(est["B"]["embedding"] is None,
       "vitima B teve o vetor APAGADO (bug reproduzido — se falhar, o pin de postgrest mudou)",
       f"B.embedding={str(est['B']['embedding'])[:40]}")
    ok(float(est["B"]["estoque"]) == 9.0, "vitima B teve o estoque aplicado (o upsert em si funcionou)")

    print("\n### 2. POS-FIX: lotes_homogeneos() preserva os dois vetores")
    semear()
    n_sublotes = 0
    for sublote in sync_erp.lotes_homogeneos(lote_misto):
        n_sublotes += 1
        sb.table(TABELA).upsert(sublote).execute()
    ok(n_sublotes == 2, "o lote misto virou 2 sub-lotes", n_sublotes)
    est = ler()
    ok(_as_list(est["A"]["embedding"]) == vec(3), "A gravou o vetor novo")
    ok(_as_list(est["B"]["embedding"]) == vec(2), "B PRESERVOU o vetor original",
       f"B.embedding={str(est['B']['embedding'])[:40]}")
    ok(float(est["B"]["estoque"]) == 9.0, "B ainda recebeu o estoque novo (fix nao perde update)")
    ok(float(est["A"]["estoque"]) == 2.0, "A ainda recebeu o estoque novo")

    print("\n### 3. lotes_homogeneos() — propriedades puras (sem rede)")
    r1 = {"id_unico": "1", "nome": "x"}
    r2 = {"id_unico": "2", "nome": "y", "embedding": [0.0]}
    r3 = {"id_unico": "3", "nome": "z"}
    grupos = sync_erp.lotes_homogeneos([r1, r2, r3])
    ok(len(grupos) == 2, "2 shapes -> 2 grupos", [sorted(g[0].keys()) for g in grupos])
    ok(sum(len(g) for g in grupos) == 3, "nenhum registro perdido")
    ok(all(len({frozenset(r.keys()) for r in g}) == 1 for g in grupos),
       "cada grupo tem um unico conjunto de chaves")
    ok(grupos[0] == [r1, r3], "ordem original preservada dentro do grupo", grupos[0])
    ok(sync_erp.lotes_homogeneos([]) == [] and sync_erp.lotes_homogeneos(None) == [],
       "lote vazio/None -> lista vazia")

finally:
    try:
        limpar()
        print("\n[cleanup] linhas de teste removidas de " + TABELA)
    except Exception as e:
        print(f"\n[cleanup] falhou: {e}")

print("\n" + ("=" * 60))
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
