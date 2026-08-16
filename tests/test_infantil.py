"""Peca INFANTIL so aparece se o cliente pedir (decisao do dono, 16/08).

O PROBLEMA, medido em 16/08 sobre 16 buscas comuns de adulto: 9 delas (56%)
traziam peca infantil, e 6 itens infantis vinham marcados `destaque` — CAMPEAO de
venda. Nao e so ruido de catalogo: numa loja de lingerie, oferecer calcinha
infantil a quem pediu calcinha e constrangedor, e o slot de campeao e o pior lugar
possivel para isso aparecer.

O MECANISMO era perverso: o ranking (0013) promove a campeao por `grade`, que e
profundidade de estoque. CALCINHA INFANTIL MALHA tem 108 pecas, CUECA INFANTIL
SRAMBOX tem 201. Ou seja, quanto MAIS infantil encalhado, mais o bot empurrava
infantil para quem pediu peca de adulto.

POR QUE PELO NOME: `nome_grupo` existe e tem categorias explicitas ('CUECA
INFANTIL', 'BABY DOLL INFANTIL', 'MODA PRAIA INFANTIL'), mas esta VAZIO em 63% das
linhas (4.263 de 6.739) e nao acusa NENHUM produto que o nome ja nao acuse — e
subconjunto estrito. O nome e o sinal.

POR QUE NAO NO `excluir_ids` DA RPC: aquele parametro tem um fallback que abandona
TODAS as exclusoes quando a lista fica curta. Correto para "ja mostrei esse card";
inaceitavel para uma politica. Por isso o corte e no re-filtro em Python.

I1  produto: o nome identifica infantil — e 'BABY DOLL' de adulto NAO casa
I2  intencao: palavra explicita, idade ate 16, tamanho numerico ate 16
I3  intencao: o que NAO pode disparar (60 anos, GG, "minha filha" sem idade)
I4  banco: busca de adulto nao traz infantil
I5  banco: quem PEDE infantil recebe infantil
I6  o knob de env desliga sem deploy

    python tests/test_infantil.py

Le o banco de producao. Nao escreve nada.
"""
import io
import contextlib
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

LOJAS = "244033,94134"
falhas = []
pulados = []


def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


def skip(nome, motivo):
    print(f"  [~] {nome}  (pulado: {motivo})")
    pulados.append(nome)


def quer(termo, msg, toks=None):
    """Roda a deteccao de intencao com o contexto de turno montado."""
    bot._set_turn_ctx("test_infantil", msg)
    try:
        return bot._cliente_quer_infantil(termo, toks)
    finally:
        bot._clear_turn_ctx()


def buscar(termo, tam, msg):
    bot._set_turn_ctx("test_infantil", msg)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return bot.consultar_estoque_supabase(termo, tamanho=tam, id_loja=LOJAS)
    finally:
        bot._clear_turn_ctx()


# =========================================================================
print("### I1 — o NOME do produto identifica infantil")
for nome in ("CUECA INFANTIL SRAMBOX", "PIJAMA MANGA INF FEM",
             "PIJAMA INF. LONGO MASC. ALGODAO", "PIJAMA JUVENIL REGATA LISTRADO M (12)",
             "BABY DOLL INFANTIL JULIA", "TOP COTTON INFANTIL M",
             "CUECA BX FEM. INFANTIL PERSONAGEM", "SUTIA INFANTIL COM BOJO"):
    ok(bot._produto_infantil(nome), f"I1 '{nome[:40]}' e infantil")

# 'BABY DOLL' e camisola de ADULTO e sao 30+ produtos. Se 'BABY' virasse sinal,
# a regra apagaria uma categoria inteira de vendas.
for nome in ("BABY DOLL SIMONE GG", "BABY DOLL RENDA", "BABY DOOL BICOLOR TAISSA",
             "SUTIA BABY BOJO PLUS", "DEDEIRA COM TEXTURA GREEN BABY",
             "CUECA ADULTO GUERRIER", "CAMISOLA JULIA"):
    ok(not bot._produto_infantil(nome),
       f"I1 '{nome[:40]}' NAO e infantil (adulto)")


# =========================================================================
print("\n### I2 — o cliente PEDIU infantil")
ok(quer("pijama infantil", "tem pijama infantil?"), "I2 palavra explicita no termo")
ok(quer("pijama juvenil", ""), "I2 'juvenil' tambem conta")
ok(quer("calcinha", "calcinha pra crianca"), "I2 'crianca' na msg crua (sem acento)")
ok(quer("calcinha", "calcinha pra criança"), "I2 ... e COM acento (normaliza igual)")
ok(quer("pijama", "quero um pijama pra minha filha de 6 anos"),
   "I2 idade <= 16 na msg crua, mesmo com o termo generico")
ok(quer("cueca", "tem cueca pro meu filho de 5 anos?"), "I2 idade em outra formulacao")
ok(quer("pijama", "pra um menino"), "I2 'menino'")
ok(quer("body", "pra minha garota de 8 aninhos"), "I2 'aninhos' conta como idade")
# O catalogo NAO tem tamanho numerico abaixo de 33 (33,35,37,40..58), entao um
# numero pequeno so pode ser crianca. E medida, nao chute.
ok(quer("cueca", "tem no tamanho 8?", ["8"]), "I2 tamanho numerico 8 -> crianca")
ok(quer("cueca", "", ["16"]), f"I2 numerico no limite ({bot.INFANTIL_IDADE_MAX})")


# =========================================================================
print("\n### I3 — o que NAO pode disparar (senao a loja perde venda de adulto)")
ok(not quer("pijama", "quero um pijama"), "I3 busca generica nao dispara")
ok(not quer("camisola", "sou senhora de 60 anos, quero camisola"),
   "I3 idade ADULTA (60 anos) nao dispara")
ok(not quer("cueca", "tem GG?", ["GG"]), "I3 tamanho de letra nao dispara")
ok(not quer("cueca", "", ["42"]), "I3 numerico ADULTO (42) nao dispara")
# Deliberado: em loja de lingerie "presente pra minha filha" e ambiguo (filha
# adulta). Errar para o lado adulto e o comportamento normal da loja; se for
# crianca, o modelo resolve e passa 'infantil' no termo (I2 cobre esse caminho).
ok(not quer("pijama", "quero um pijama pra minha filha"),
   "I3 parentesco SEM idade nao dispara (ambiguo de proposito)")
ok(not quer("conjunto", "pro meu neto"), "I3 idem para 'neto'")


# =========================================================================
print("\n### I4/I5 — contra o banco de verdade")
ADULTAS = [("cueca", "GG"), ("cueca", "M"), ("pijama", "M"), ("pijama", "G"),
           ("calcinha", "P"), ("calcinha", "M"), ("sutia", "G"), ("top", "M"),
           ("short doll", "M"), ("camisola", "M")]
try:
    vazou = []
    medidas = 0
    for termo, tam in ADULTAS:
        r = buscar(termo, tam, f"tem {termo} tamanho {tam}?")
        if r.get("status") != "sucesso":
            continue
        medidas += 1
        kids = [p.get("nome") for p in (r.get("produtos") or [])
                if bot._produto_infantil(p.get("nome"))]
        if kids:
            vazou.append((termo, tam, kids))
    if medidas < len(ADULTAS) // 2:
        skip("I4", f"so {medidas} de {len(ADULTAS)} buscas responderam")
    else:
        ok(not vazou,
           f"I4 nenhuma das {medidas} buscas de ADULTO traz infantil (antes: 9/16)",
           vazou[:3])

    r = buscar("pijama infantil", "M", "tem pijama infantil pro meu filho de 6 anos?")
    if r.get("status") != "sucesso":
        skip("I5", f"busca devolveu {r.get('status')}")
    else:
        ps = r.get("produtos") or []
        kids = [p for p in ps if bot._produto_infantil(p.get("nome"))]
        ok(len(kids) > 0,
           "I5 quem PEDE infantil RECEBE infantil (o filtro nao e cego)",
           [(p.get("nome") or "")[:30] for p in ps])
        ok(len(kids) >= len(ps) // 2,
           "I5 ... e infantil e a MAIORIA da lista, nao uma sobra",
           f"{len(kids)}/{len(ps)}")
except Exception as e:
    skip("I4/I5", f"{type(e).__name__}: {str(e)[:70]}")


# =========================================================================
print("\n### I6 — knob de env (ajuste sem deploy)")
_orig = bot.INFANTIL_SO_SE_PEDIR
try:
    bot.INFANTIL_SO_SE_PEDIR = False
    r = buscar("pijama", "M", "tem pijama tamanho M?")
    if r.get("status") != "sucesso":
        skip("I6", f"busca devolveu {r.get('status')}")
    else:
        kids = [p.get("nome") for p in (r.get("produtos") or [])
                if bot._produto_infantil(p.get("nome"))]
        ok(len(kids) > 0,
           "I6 INFANTIL_SO_SE_PEDIR=0 volta ao comportamento antigo (infantil passa)",
           kids)
finally:
    bot.INFANTIL_SO_SE_PEDIR = _orig
ok(bot.INFANTIL_SO_SE_PEDIR is True, "I6 knob restaurado ao default (ligado)")


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
