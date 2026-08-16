"""TAMANHO UNICO entra nas buscas com tamanho (decisao do dono, 16/08 — opcao A).

Peca de tamanho unico tem REGULAGEM e veste do P ao GG. Antes disto ela sumia de
TODA busca que informasse tamanho, porque o filtro casa por overlap de tokens e
'UNICO' nunca faz overlap com 'M'. Custo medido: 229 produtos com estoque (8 deles
campeoes de venda) invisiveis em 76% das buscas — a fatia que informa tamanho,
segundo tool_filtro_eventos.

COMO FOI FEITO, e por que NAO tem migration: a RPC ja passa `filtro_tamanho` por
normalize_tamanho_tokens, que quebra em '/'. Entao mandar "M/UNICO" produz os tokens
{M, UNICO} e o overlap casa os dois. O re-filtro em Python deriva os tokens da MESMA
string, entao os dois filtros ficam espelhados POR CONSTRUCAO — que e exatamente o
invariante cuja violacao produziu o bug dos 623 acentuados.

U1  politica: entra em P/M/G/GG e numerico ate UNICO_NUMERICO_MAX
U2  politica: NAO entra acima de GG (G1..G3, XG, XGG, EG, 2EG, 48+)
U3  politica: nao expande busca sem tamanho, nem busca que JA e por unico
U4  os dois filtros espelhados: tokens_alvo ganha UNICO junto com a string da RPC
U5  banco de verdade: busca por M traz peca UNICO; busca por 56 nao traz
U6  filtro_aplicado.tamanho continua sendo o que o CLIENTE pediu (nao "M/UNICO")
U7  instrucao dinamica avisa o modelo para nao descartar a peca
U8  os knobs de env desligam/ajustam sem deploy
U9  o prompt explica a regulagem
U10 'ÚNICO' FALSO: 27 pecas tem o tamanho real no fim do nome e sao descartadas

    python tests/test_tamanho_unico.py

Le o banco de producao. Nao escreve nada.
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


def inc(t):
    return bot._inclui_unico(set(bot._tokens_tamanho(t)))


def eh_unico(t):
    return "UNICO" in bot._tokens_tamanho(t)


# =========================================================================
print("### U1/U2/U3 — a politica de ate onde a regulagem alcanca")
for t in ("P", "M", "G", "GG"):
    ok(inc(t), f"U1 pediu {t} -> inclui tamanho unico (regulagem veste P..GG)")
for t in ("40", "42", "44", "46"):
    ok(inc(t), f"U1 pediu {t} (numerico ate {bot.UNICO_NUMERICO_MAX}) -> inclui")

for t in ("G1", "G2", "G3", "XG", "XGG", "EG", "2EG"):
    ok(not inc(t), f"U2 pediu {t} -> NAO inclui (acima de GG a regulagem nao alcanca)")
for t in ("48", "50", "52", "54", "56"):
    ok(not inc(t), f"U2 pediu {t} -> NAO inclui (numerico acima do corte)")

ok(not bot._inclui_unico(set()), "U3 busca SEM tamanho nao expande (ja traz tudo)")
ok(not inc("UNICO"), "U3 busca que JA e por unico nao se expande")
ok(not inc("ÚNICO"), "U3 idem com acento (normaliza para UNICO)")


# =========================================================================
print("\n### U4 — os dois filtros (SQL e Python) espelhados por construcao")
# A string que vai para a RPC e a fonte dos tokens do re-filtro: se ela leva UNICO,
# os tokens levam tambem. E o que impede a divergencia do bug dos 623.
toks_rpc = set(bot._tokens_tamanho("M/UNICO"))
ok(toks_rpc == {"M", "UNICO"},
   "U4 'M/UNICO' normaliza em {M, UNICO} — mesma funcao dos dois lados", toks_rpc)
ok(set(bot._tokens_tamanho("GG/UNICO")) == {"GG", "UNICO"},
   "U4 idem para GG")


# =========================================================================
print("\n### U5/U6/U7 — contra o banco de verdade")
# A sonda e 'fantasia', nao 'camisola'. Motivo achado em 16/08: TODA camisola
# cadastrada como 'ÚNICO' na verdade tem o tamanho no nome ('...RENDA GG'), entao
# depois do descarte do U10 nao sobra unico legitimo naquela categoria — a busca
# por camisola M passou a ser uma sonda que nao prova nada. Em fantasia/sexshop o
# 'ÚNICO' e de verdade.
try:
    r = bot.consultar_estoque_supabase("fantasia", tamanho="M", id_loja=LOJAS)
    if r.get("status") != "sucesso":
        skip("U5/U6/U7", f"busca devolveu {r.get('status')}")
    else:
        ps = r.get("produtos") or []
        fa = r.get("filtro_aplicado") or {}
        n_uni = sum(1 for p in ps if eh_unico(p.get("tamanho")))
        ok(n_uni > 0, "U5 busca por M traz peca de tamanho UNICO (antes: nenhuma)",
           [p.get("tamanho") for p in ps])
        ok(fa.get("tamanho") == "M",
           "U6 filtro_aplicado.tamanho e o que o CLIENTE pediu, nao 'M/UNICO'",
           fa.get("tamanho"))
        ok("UNICO" in (fa.get("tamanho_tokens") or []),
           "U6 mas tamanho_tokens revela UNICO (a verdade do que foi consultado)",
           fa.get("tamanho_tokens"))
        instr = fa.get("instrucao_tamanho_unico") or ""
        ok("REGULAGEM" in instr.upper(),
           "U7 instrucao dinamica explica a regulagem", instr[:80])
        ok("NAO descarte" in instr or "nao descarte" in instr.lower(),
           "U7 instrucao manda NAO descartar (senao o §4 faria o modelo curar fora)",
           instr[:80])

    r56 = bot.consultar_estoque_supabase("camisola", tamanho="56", id_loja=LOJAS)
    if r56.get("status") == "sucesso":
        ps56 = r56.get("produtos") or []
        ok(all(not eh_unico(p.get("tamanho")) for p in ps56),
           "U5 busca por 56 NAO traz unico (nao gasta slot com o que nao serve)",
           [p.get("tamanho") for p in ps56])
        ok("instrucao_tamanho_unico" not in (r56.get("filtro_aplicado") or {}),
           "U7 sem unico na lista, sem instrucao (custo de token zero)")
    else:
        skip("U5 (56)", f"busca devolveu {r56.get('status')}")
except Exception as e:
    skip("U5/U6/U7", f"{type(e).__name__}: {str(e)[:70]}")


# =========================================================================
# ACHADO EM 16/08, depois de a expansao ja estar escrita: nem todo 'ÚNICO' do ERP
# e peca com regulagem. 27 produtos de vestuario tem tamanho='ÚNICO' e o tamanho
# REAL escrito no fim do nome ('CAMISOLA DE URDA XGG', 'TOP COTTON INFANTIL P').
# Metade e GG/XGG — acima do que a propria politica diz que a regulagem alcanca.
#
# Sem o descarte, a busca por M devolvia a XGG *com a instrucao dinamica jurando
# que ela veste do P ao GG*. Mentir o tamanho para o cliente e pior do que a peca
# nao aparecer, entao aqui a peca sai.
print("\n### U10 — 'ÚNICO' falso: tamanho real escondido no nome do produto")

for nome, esperado in (
    ("CAMISOLA DE URDA XGG", "XGG"),
    ("BABY DOOL RENDA GG", "GG"),
    ("TOP COTTON INFANTIL P", "P"),
    ("PIJAMA JUVENIL REGATA LISTRADO GG(16)", "GG"),     # numeracao infantil junto
    ("PIJAMA JUVENIL REGATA LISTRADO M (12)", "M"),      # ... e com espaco
):
    got = bot._tamanho_no_nome(nome)
    ok(got == esperado, f"U10 '{nome[:38]}' -> tamanho real {esperado}", got)

# O ancora no FIM da string e o que separa isto de destruir o sexshop: 'PONTO G' e
# 'PLUG ANAL M' tem letra de tamanho no MEIO do nome e sao produto de verdade.
for nome in ("VIBRADOR PONTO G GOLFINHO", "PLUG ANAL M ACO BRILHANTE SEXY IMPORT",
             "ICE GEL CORPORAL 15 ML SABORES FOR SEXY", "FANTASIA LUXO DIVERSAS",
             "TANGA CAROLZINHA", "PETALAS PERFUMADAS 60 UNIDADES SEXY FANTASY"):
    ok(bot._tamanho_no_nome(nome) is None,
       f"U10 '{nome[:38]}' NAO e falso positivo (tamanho no meio, ou nenhum)",
       bot._tamanho_no_nome(nome))

try:
    r = bot.consultar_estoque_supabase("camisola", tamanho="M", id_loja=LOJAS)
    if r.get("status") != "sucesso":
        skip("U10 (banco)", f"busca devolveu {r.get('status')}")
    else:
        ps = r.get("produtos") or []
        mentirosos = [(p.get("nome"), p.get("tamanho")) for p in ps
                      if eh_unico(p.get("tamanho"))
                      and (bot._tamanho_no_nome(p.get("nome")) or "M") != "M"]
        ok(not mentirosos,
           "U10 busca por M nao devolve peca 'UNICO' cujo nome diz outro tamanho",
           mentirosos)
        # E o inverso continua valendo: unico LEGITIMO nao pode ter sido levado junto
        # pelo descarte. Se o catalogo mudar e nao houver nenhum, isto vira skip.
        r_f = bot.consultar_estoque_supabase("fantasia", tamanho="M", id_loja=LOJAS)
        if r_f.get("status") == "sucesso":
            legitimos = [p.get("nome") for p in (r_f.get("produtos") or [])
                         if eh_unico(p.get("tamanho"))
                         and bot._tamanho_no_nome(p.get("nome")) is None]
            if legitimos:
                ok(True, f"U10 unico LEGITIMO sobrevive ({legitimos[0][:34]})")
            else:
                skip("U10 (legitimo)", "nenhum unico legitimo nesta busca hoje")
        else:
            skip("U10 (legitimo)", f"busca devolveu {r_f.get('status')}")
except Exception as e:
    skip("U10 (banco)", f"{type(e).__name__}: {str(e)[:70]}")


# =========================================================================
print("\n### U8 — knobs de env (ajuste sem deploy)")
_orig = (bot.UNICO_ATENDE, bot.UNICO_NUMERICO_MAX, set(bot.UNICO_NAO_ATENDE))
try:
    bot.UNICO_ATENDE = False
    ok(not inc("M"), "U8 UNICO_ATENDE=0 desliga tudo")
    bot.UNICO_ATENDE = True

    bot.UNICO_NUMERICO_MAX = 50
    ok(inc("48") and inc("50"), "U8 UNICO_NUMERICO_MAX=50 passa a incluir 48 e 50")
    ok(not inc("52"), "U8 ... e 52 continua de fora")
    bot.UNICO_NUMERICO_MAX = _orig[1]

    bot.UNICO_NAO_ATENDE = set()
    ok(inc("G3"), "U8 blocklist vazia passa a incluir G3")
finally:
    bot.UNICO_ATENDE, bot.UNICO_NUMERICO_MAX, bot.UNICO_NAO_ATENDE = _orig
    ok(inc("M") and not inc("G3"), "U8 knobs restaurados ao default")


# =========================================================================
print("\n### U9 — o prompt explica a regulagem")
with open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8") as f:
    prompt = f.read()
ok("REGULAGEM" in prompt.upper(), "U9 prompt fala de regulagem")
ok("não descarte" in prompt.lower(),
   "U9 prompt manda NAO descartar a peca de tamanho unico")
ok("de propósito" in prompt or "de proposito" in prompt,
   "U9 prompt explica que a peca aparece DE PROPOSITO na busca com tamanho")


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
