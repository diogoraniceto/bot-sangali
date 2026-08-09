"""Frente 2 — verdade no retorno das tools. Tamanho: fonte, tokens e overlap.

Nao faz chamada de rede: so funcoes puras de bot.py + fixture congelada do banco.
O bloco final (opcional) confere a fixture contra `produtos_estoque` se houver .env.

    python tests/test_tamanho_tokens.py
"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["ENABLE_INPROCESS_SYNC"] = "0"       # nao sobe scheduler/watchdog
os.environ.setdefault("WHATSAPP_PROVIDER", "uazapi")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))
import bot

falhas = []
def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


UNICO_ACENTO = chr(218) + "NICO"        # 'ÚNICO' — o payload de curl no Git Bash mangla o U-acute

# Legendas REAIS de card (card_envios / scenarios.json). O preco tem parte inteira
# na faixa 30-69, exatamente a faixa do regex de tamanho numerico.
PREAMBULO = ("[o cliente respondeu ao card do produto id_produto=6743024. "
             "Use EXATAMENTE id_produto=6743024 para este item em calcular_total / "
             "produtos_recomendados / resumo; nao re-derive o id pelo nome.] ")
LEGENDA_VAREJO = "*CAMISOLA DE URDA* - R$ 49,90\n_cod: 6743024_ "
PREAMBULO_UAZAPI = ('[o cliente respondeu ao card do produto id_produto=6743024 '
                    '(citacao: "*Conjunto Gold Be* - R$ 54,50 _cod: 6743024_"). '
                    'Use EXATAMENTE id_produto=6743024 para este item em calcular_total / '
                    'produtos_recomendados / resumo; nao re-derive o id pelo nome.] ')
LEGENDA_ATACADO = ("*CONJUNTO GOLD BE*\nAtacado à vista: *R$ 31,15*\n"
                   "(de R$ 44,50 no varejo)\n_cód: 6743024_ ")


print("### A. Preco de card NAO pode virar tamanho (bug critico)")

# A1 — prova do bug no texto CRU (o que o guard lia antes deste commit)
ok(bot._tamanhos_validos_na_msg(PREAMBULO + LEGENDA_VAREJO + "tem no M?") == ["49", "M"],
   "A1 texto CRU ainda extrai o preco 49 como tamanho (bug reproduzido)",
   bot._tamanhos_validos_na_msg(PREAMBULO + LEGENDA_VAREJO + "tem no M?"))
ok(bot._tamanhos_validos_na_msg(PREAMBULO_UAZAPI + LEGENDA_ATACADO + "tem P?")[:1] != ["P"],
   "A1b texto CRU do atacado poe numeros de preco antes das letras",
   bot._tamanhos_validos_na_msg(PREAMBULO_UAZAPI + LEGENDA_ATACADO + "tem P?"))

# A2 — depois do saneamento so sobra o que o cliente escreveu
casos_puro = [
    (PREAMBULO + LEGENDA_VAREJO + "tem no M?", ["M"]),
    (PREAMBULO_UAZAPI + LEGENDA_ATACADO + "tem P?", ["P"]),
    (PREAMBULO + LEGENDA_VAREJO + "tem?", []),                 # cliente nao falou tamanho
    (PREAMBULO_UAZAPI + "quero fechar 10 desse a vista", []),
    ("[transcrição de áudio do cliente:] oi, queria uma camisola G", ["G"]),
    ("[o cliente enviou uma foto que parece: camisola preta de renda] "
     'Legenda do cliente: "tem no GG?"', ["GG"]),
    ("quero um pijama 42", ["42"]),                             # tamanho numerico REAL sobrevive
    ("tem por R$ 49,90 no G?", ["G"]),                          # preco escrito pelo cliente
    ("[respondendo a mensagem citada: \"*BABY DOLL* - R$ 58,00 _cod: 8060563_\"] gostei, tem M?", ["M"]),
]
for txt, esperado in casos_puro:
    got = bot._tamanhos_validos_na_msg(bot._texto_cliente_puro(txt))
    ok(got == esperado, f"A2 puro {txt[-38:]!r} -> {esperado}", got)

# A3 — o guard, que e quem forcava o preco. Antes: forcava '49'. Depois: nao mexe.
bot._set_turn_ctx("u_test", PREAMBULO + LEGENDA_VAREJO + "tem?")
ok(bot._tokens_tamanho_do_cliente() == [],
   "A3 tokens do cliente vazios quando ele nao falou tamanho", bot._tokens_tamanho_do_cliente())
ok(bot._resolver_tamanho_alvo("G") == ("G", False),
   "A3 guard NAO forca preco '49' quando o LLM passa 'G' de contexto",
   bot._resolver_tamanho_alvo("G"))

bot._set_turn_ctx("u_test", PREAMBULO_UAZAPI + LEGENDA_ATACADO + "tem?")
ok(bot._resolver_tamanho_alvo("M") == ("M", False),
   "A3b guard NAO forca preco '31'/'44' no card de atacado", bot._resolver_tamanho_alvo("M"))

# A4 — o guard continua funcionando para o caso que ele existe para resolver
bot._set_turn_ctx("u_test", PREAMBULO + LEGENDA_VAREJO + "tem no G?")
ok(bot._resolver_tamanho_alvo("GG") == ("G", True),
   "A4 guard ainda corrige GG->G quando o cliente escreveu G", bot._resolver_tamanho_alvo("GG"))
ok(bot._resolver_tamanho_alvo("G") == ("G", False),
   "A4b guard nao mexe quando o LLM acerta", bot._resolver_tamanho_alvo("G"))
bot._clear_turn_ctx()
ok(bot._ctx_msg_cliente() == "", "A4c fora de turno nao ha fala do cliente", bot._ctx_msg_cliente())
ok(bot._resolver_tamanho_alvo("GG") == ("GG", False),
   "A4d fora de turno o guard nao inventa correcao", bot._resolver_tamanho_alvo("GG"))


print("\n### B. _tokens_tamanho espelha normalize_tamanho_tokens (SQL)")

# Os 35 valores DISTINTOS de produtos_estoque.tamanho e seus tamanho_tokens,
# lidos do banco em 2026-08-09 (0 linhas com token stale -> fixture fiel).
FIXTURE = [
    ("", []),
    ("120", ["120"]), ("130", ["130"]), ("150", ["150"]),
    ("2EG", ["2EG"]),
    ("33", ["33"]), ("35", ["35"]), ("37", ["37"]), ("40", ["40"]), ("42", ["42"]),
    ("44", ["44"]), ("46", ["46"]), ("48", ["48"]), ("50", ["50"]), ("52", ["52"]),
    ("54", ["54"]), ("56", ["56"]), ("58", ["58"]),
    ("EG", ["EG"]),
    ("G", ["G"]), ("G1", ["G1"]), ("G2", ["G2"]), ("G3", ["G3"]), ("G4", ["G4"]),
    ("GG", ["GG"]), ("G/GG", ["G", "GG"]),
    ("M", ["M"]), ("P", ["P"]), ("P/M", ["P", "M"]), ("PP", ["PP"]),
    ("UNICO", ["UNICO"]), (UNICO_ACENTO, ["UNICO"]),
    ("XG", ["XG"]), ("XGG", ["XGG"]), ("XL", ["XL"]),
]
ok(len(FIXTURE) == 35, "B0 fixture tem os 35 valores distintos do catalogo", len(FIXTURE))
for tam, esperado in FIXTURE:
    got = bot._tokens_tamanho(tam)
    ok(got == esperado, f"B1 _tokens_tamanho({tam!r}) == {esperado}", got)

# Casos exigidos pelo plano que nao estao no catalogo hoje
for tam, esperado in [("38-40", ["38", "40"]), ("TAM 42", ["TAM", "42"]),
                      ("p/m", ["P", "M"]), ("  G  ", ["G"]), (None, []),
                      ("P, M", ["P", "M"]), ("G (GG)", ["G", "GG"]), ("A|B", ["A", "B"])]:
    got = bot._tokens_tamanho(tam)
    ok(got == esperado, f"B2 _tokens_tamanho({tam!r}) == {esperado}", got)


print("\n### C. Overlap libera composto/acento sem vazar tamanho errado")

def overlap(alvo, tamanho_produto):
    """Reproduz a decisao de `consultar_estoque_supabase` (passo 3)."""
    tokens_alvo = set(bot._tokens_tamanho(alvo)) if alvo else set()
    if not alvo:
        return True
    toks_p = set(bot._tokens_tamanho(tamanho_produto))
    return (not tokens_alvo) or bool(tokens_alvo & toks_p)

def igualdade_legado(alvo, tamanho_produto):
    """O que o codigo ANTIGO fazia: p['tamanho'].upper() == tamanho_alvo."""
    return (tamanho_produto or "").upper() == alvo

LIBERA = [("UNICO", UNICO_ACENTO), ("M", "P/M"), ("P", "P/M"),
          ("G", "G/GG"), ("GG", "G/GG")]
for alvo, tam_prod in LIBERA:
    ok(overlap(alvo, tam_prod) and not igualdade_legado(alvo, tam_prod),
       f"C1 alvo {alvo!r} aceita produto {tam_prod!r} (a igualdade derrubava)",
       (overlap(alvo, tam_prod), igualdade_legado(alvo, tam_prod)))

# A expansao que o prompt PROIBE nao volta: conjuntos disjuntos continuam disjuntos.
NAO_LIBERA = [("G", "GG"), ("GG", "G"), ("P", "PP"), ("PP", "P"), ("M", "G"),
              ("G", "G1"), ("G", "XG"), ("G", "EG"), ("42", "44"), ("40", "140"),
              ("M", "UNICO"), ("UNICO", "M"), ("P", "G/GG"), ("GG", "P/M")]
for alvo, tam_prod in NAO_LIBERA:
    ok(not overlap(alvo, tam_prod),
       f"C2 alvo {alvo!r} REJEITA produto {tam_prod!r}", overlap(alvo, tam_prod))

ok(overlap(None, "G/GG") and overlap(None, ""),
   "C3 sem tamanho pedido nada e filtrado")
# `not tokens_alvo` espelha o branch cardinality(...)=0 da RPC (0009:54)
ok(bot._tokens_tamanho("/") == [] and overlap("/", "G"),
   "C4 alvo que normaliza para 0 tokens nao filtra nada (espelha cardinality=0 da RPC)")


print("\n### D. Fixture x banco (opcional; so com .env)")
if not os.getenv("SUPABASE_URL"):
    print("  [~] sem SUPABASE_URL — camada de banco pulada")
else:
    try:
        r = (bot.supabase.table("produtos_estoque")
             .select("tamanho, tamanho_tokens").limit(20000).execute())
        vistos = {}
        for row in r.data or []:
            vistos[row.get("tamanho") or ""] = row.get("tamanho_tokens") or []
        # PostgREST limita a pagina, entao a amostra pode nao conter os valores raros.
        # O contrato que importa: nenhum valor do banco fora da fixture.
        novos = sorted(set(vistos) - {t for t, _ in FIXTURE})
        ok(not novos, "D1 nenhum tamanho do banco esta fora da fixture", novos)
        print(f"      (amostra: {len(r.data or [])} linhas, {len(vistos)} tamanhos distintos "
              f"de 35; nao observados: {sorted({t for t, _ in FIXTURE} - set(vistos))})")
        divergentes = [(t, tk, bot._tokens_tamanho(t))
                       for t, tk in vistos.items() if bot._tokens_tamanho(t) != tk]
        ok(not divergentes, "D2 _tokens_tamanho == tamanho_tokens do banco em 100% dos valores",
           divergentes)
    except Exception as e:
        print(f"  [~] banco indisponivel ({type(e).__name__}: {str(e)[:80]}) — pulado")


print("\n" + ("=" * 60))
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
