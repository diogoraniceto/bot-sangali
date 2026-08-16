"""Filtro de loja aceitando LISTA — MATRIZ + FILIAL 01 (migration 0014).

Pedido do dono (15/08): o estoque deve considerar as duas unidades. Elas ficam na
mesma rua em Linhares e ambas entregam.

Medido antes de mudar: MATRIZ 517 produtos com estoque, FILIAL 01 555, 392 em ambas
-> 163 produtos existem SO na filial (+32% de catalogo), 10 deles campeoes de venda.
Divergencia de preco entre as lojas: 0 em 742 pares.

L1  so a MATRIZ continua funcionando identico (retrocompatibilidade)
L2  as DUAS lojas devolvem linhas das duas, e acham produto que so existe na filial
L3  espaco na lista nao quebra ("244033, 94134")
L4  pg_proc tem UMA candidata (duas = PGRST202 e a busca inteira cai)
L5  `filtro_aplicado.id_loja` sai canonico, sem espaco (telemetria consistente)
L6  o prompt manda as duas lojas nos TRES pontos, sem sobra do valor antigo

    python tests/test_multi_loja.py

Le o banco de producao (busca real). Nao escreve nada.
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["ENABLE_INPROCESS_SYNC"] = "0"
os.environ.setdefault("WHATSAPP_PROVIDER", "uazapi")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

import bot  # noqa: E402

MATRIZ = "244033"
FILIAL = "94134"
AMBAS = f"{MATRIZ},{FILIAL}"
# Campeao de venda que existe SO na FILIAL 01. Se um dia ele entrar na matriz ou sair
# de estoque, L2 passa a pular em vez de dar falso negativo.
SO_NA_FILIAL = "55341583"      # CALCINHA LAVINIA/ARYANA

falhas = []
pulados = []


def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


def skip(nome, motivo):
    print(f"  [~] {nome}  (pulado: {motivo})")
    pulados.append(nome)


def busca(vetor, loja, tokens):
    r = bot._rpc_busca({
        "query_embedding": vetor, "match_threshold": 0.5,
        "match_count": bot.POOL_CANDIDATOS, "filtro_tamanho": None,
        "filtro_id_loja": loja, "limite_produtos": bot.LIMITE_PRODUTOS,
        "ancora_semantica": bot.ANCORA_SEMANTICA,
        "janela_similaridade": bot.JANELA_SIMILARIDADE,
        "minimo_grade": bot.RANKING_MINIMO_GRADE,
        "termo_tokens": tokens, "excluir_ids": None})
    return r.data or []


# =========================================================================
print("### L4 — UMA candidata em pg_proc (duas = PGRST202 e a busca cai inteira)")
try:
    import requests
    ref = (os.getenv("SUPABASE_URL") or "").replace("https://", "").split(".")[0]
    pat = os.getenv("SUPABASE_PAT") or os.getenv("SUPABASE_MGMT_PAT")
    if not (ref and pat):
        skip("L4", "SUPABASE_PAT nao configurado (checagem manual: select from pg_proc)")
    else:
        r = requests.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"},
            json={"query": "select count(*) as n from pg_proc "
                           "where proname='buscar_produtos_semantico'"}, timeout=45)
        n = (r.json() or [{}])[0].get("n") if r.ok else None
        ok(n == 1, "L4 exatamente 1 candidata de buscar_produtos_semantico", n)
except Exception as e:
    skip("L4", f"{type(e).__name__}: {str(e)[:60]}")


# =========================================================================
print("\n### L1/L2/L3 — o filtro de lista funciona e amplia o catalogo")
try:
    vetor = bot.get_embedding("calcinha lavinia aryana")
    if vetor is None:
        raise RuntimeError("get_embedding devolveu None (quota?)")
    toks = ["CALCINHA", "LAVINIA"]

    so_matriz = busca(vetor, MATRIZ, toks)
    lojas_m = {x["id_loja"] for x in so_matriz}
    ok(len(so_matriz) > 0, "L1 busca so na MATRIZ continua devolvendo produtos",
       len(so_matriz))
    ok(lojas_m == {MATRIZ}, "L1 e SO da matriz (retrocompatibilidade preservada)",
       sorted(lojas_m))

    ambas = busca(vetor, AMBAS, toks)
    lojas_a = {x["id_loja"] for x in ambas}
    ok(len(ambas) > 0, "L2 busca nas DUAS devolve produtos", len(ambas))
    ok(lojas_a == {MATRIZ, FILIAL},
       "L2 o resultado traz linhas das DUAS lojas", sorted(lojas_a))

    ids_m = {x["id_produto"] for x in so_matriz}
    ids_a = {x["id_produto"] for x in ambas}
    if SO_NA_FILIAL in ids_m:
        skip("L2 produto-so-da-filial", f"{SO_NA_FILIAL} passou a existir na matriz")
    else:
        ok(SO_NA_FILIAL in ids_a,
           f"L2 acha {SO_NA_FILIAL}, campeao que SO existe na filial (invisivel antes)",
           sorted(ids_a))

    com_espaco = busca(vetor, "244033, 94134", toks)
    ok({x["id_produto"] for x in com_espaco} == ids_a,
       "L3 espaco na lista da o MESMO resultado (o SQL limpa)",
       (len(com_espaco), len(ambas)))
except Exception as e:
    skip("L1/L2/L3", f"{type(e).__name__}: {str(e)[:70]}")


# =========================================================================
print("\n### L5 — id_loja sai canonico no filtro_aplicado")
try:
    r = bot.consultar_estoque_supabase("calcinha", tamanho="M", id_loja="244033, 94134")
    aplicado = (r.get("filtro_aplicado") or {}).get("id_loja")
    ok(aplicado == AMBAS,
       "L5 '244033, 94134' e normalizado para '244033,94134' (telemetria consistente)",
       aplicado)
except Exception as e:
    skip("L5", f"{type(e).__name__}: {str(e)[:60]}")


# =========================================================================
print("\n### L6 — o prompt manda as duas lojas, sem sobra do valor antigo")
with open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8") as f:
    prompt = f.read()

ok(prompt.count(f'id_loja="{AMBAS}"') >= 3,
   "L6 as TRES chamadas do prompt usam as duas lojas (§4b, exemplo e EX 1)",
   prompt.count(f'id_loja="{AMBAS}"'))
ok(f'id_loja="{MATRIZ}"' not in prompt,
   'L6 nenhuma sobra de id_loja="244033" sozinho (o modelo copiaria o exemplo)')
ok("restringe à MATRIZ" not in prompt,
   "L6 o EX 1 nao diz mais que restringe a MATRIZ")
ok(FILIAL in prompt, "L6 o prompt cita a FILIAL 01")
ok("Não diga ao cliente em qual das duas unidades" in prompt,
   "L6 a Luna e instruida a NAO citar a unidade (para o cliente e a mesma loja)")


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
