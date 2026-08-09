"""F3 — fotos sob demanda: testes de unidade (A-J).

Bate no Supabase REAL (leitura de produtos_imagens/produtos_estoque) e faz STUB
dos dois envios de WhatsApp — NUNCA envia mensagem de verdade. Nao escreve nada
no banco: a tool e read-only e o sender so envia (stubbado).

    python tests/test_fotos_demanda.py

A  ordem determinística de `_fotos_do_produto` == `order by id`
B  idempotencia no turno: 2 chamadas da tool -> 1 pid, 4 URLs distintas; 2o
   sender envia 0
C  paginacao: turno novo manda a 5a foto; 3o turno devolve `sem_novas`
D  a foto do CARD nao e reenviada como extra (5 midias, 5 URLs distintas)
E  produto de 1 foto: `ok`/`vai_enviar 1`; no turno seguinte `sem_novas`
F  apos o card do mesmo produto a tool devolve `ja_vistas: 1` (chave `pid`
   consistente entre gravacao e leitura)
G  2 pids no mesmo turno -> o 2o devolve `limite_turno`
H  a tool funciona DE DENTRO da thread de trabalho (`_ia_send_com_teto`) e o pid
   coletado la e visto por quem espera
I  thread ABANDONADA: depois de `fechar()` a tool nao consegue mais agendar envio,
   e um segundo sender nao manda nada
J  produto SEM foto -> `sem_foto`; turno que falha descarta os pedidos de foto
"""
import os
import sys
import threading

os.environ["ENABLE_INPROCESS_SYNC"] = "0"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

import bot  # noqa: E402

# --- stubs de envio, instalados ANTES de qualquer chamada -------------------
_midias = []      # [{"to","url","caption"}]
_textos = []


def _stub_midia(numero, url, legenda):
    _midias.append({"to": numero, "url": url, "caption": legenda or ""})
    return {"messageid": "FAKE", "status": "stubbed"}   # dict de propósito: prova
                                                       # que o isinstance(str) do
                                                       # sender evita card_envios


def _stub_texto(numero, texto):
    _textos.append({"to": numero, "text": texto or ""})
    return {"messageid": "FAKE", "status": "stubbed"}


bot.enviar_midia_whatsapp = _stub_midia
bot.enviar_mensagem_whatsapp = _stub_texto
bot.FOTOS_SLEEP_SEG = 0.0        # sem sleep no teste

# Produtos-fixture (contagens verificadas no banco em 2026-08-09)
PID_5 = "8147660"      # 5 fotos
PID_10 = "8060563"     # 10 fotos
PID_1 = "63252641"     # 1 foto
PID_0 = "999999999"    # inexistente -> sem foto

RES = {"pass": 0, "fail": 0}


def ok(nome, cond, info=""):
    marca = "[+]" if cond else "[-]"
    RES["pass" if cond else "fail"] += 1
    print(f"  {marca} {nome} {info}")


def reset(uid=None):
    _midias.clear()
    _textos.clear()
    if uid is not None:
        with bot._fotos_vistas_lock:
            bot._fotos_vistas.pop(uid, None)


def urls():
    return [m["url"] for m in _midias]


# --------------------------------------------------------------------------- A
def teste_a():
    print("\n### A. ordem deterministica (id ASC)")
    f5 = bot._fotos_do_produto(PID_5)
    f10 = bot._fotos_do_produto(PID_10)
    f1 = bot._fotos_do_produto(PID_1)
    ok("A1 5 fotos", len(f5) == 5, f"len={len(f5)}")
    ok("A2 10 fotos", len(f10) == 10, f"len={len(f10)}")
    ok("A3 1 foto", len(f1) == 1, f"len={len(f1)}")
    ok("A4 sem duplicata", len(set(f10)) == len(f10))
    ok("A5 estavel entre chamadas", bot._fotos_do_produto(PID_10) == f10)
    # a ordem tem de ser a do `order by id` — comparada com uma leitura crua
    raw = (bot.supabase.table("produtos_imagens").select("id, imagem_url, imagem_mini_url")
           .eq("produto_id", PID_10).order("id", desc=False).execute().data or [])
    esperado = [(r.get("imagem_url") or r.get("imagem_mini_url")) for r in raw]
    ok("A6 ordem == order by id", esperado == f10)
    # normalizacao do pid: o Gemini manda 54557957.0
    ok("A7 pid float normalizado", bot._fotos_do_produto(float(PID_10)) == f10)
    ok("A8 pid invalido nao levanta", bot._fotos_do_produto("abc") == [])


# --------------------------------------------------------------------------- B
def teste_b():
    print("\n### B. idempotencia no turno (2 chamadas da tool, 2 chamadas do sender)")
    uid = "t_fotos_b"
    reset(uid)
    pend = bot._FotosPendentes()
    tool = bot.criar_tool_mostrar_fotos(uid, pend)
    r1 = tool(PID_5)
    r2 = tool(PID_5)              # o modelo chama de novo / retry replaya
    r3 = tool(float(PID_5))       # mesmo produto, formato float
    ok("B1 status ok", r1["status"] == "ok", r1["status"])
    ok("B2 payload identico no replay", (r1 == r2 == r3), f"{r2['status']}/{r3['status']}")
    ok("B3 pid entrou UMA vez", len(pend) == 1, f"len={len(pend)}")
    ok("B4 vai_enviar = teto", r1["vai_enviar"] == bot.FOTOS_MAX_POR_PEDIDO, r1["vai_enviar"])
    ok("B5 restantes = 5 - teto", r1["restantes"] == 5 - bot.FOTOS_MAX_POR_PEDIDO, r1["restantes"])

    n = bot._enviar_fotos_extras_pendentes(uid, pend)
    ok("B6 sender enviou vai_enviar", n == r1["vai_enviar"], f"n={n}")
    ok("B7 URLs distintas", len(set(urls())) == len(urls()) == n, f"{len(set(urls()))}/{len(urls())}")
    ok("B8 legenda da 1a anuncia neutro", "ângulo" in _midias[0]["caption"].lower()
       or "ângulos" in _midias[0]["caption"].lower(), _midias[0]["caption"][:40])
    ok("B9 nenhuma legenda diz 'de tras'/'frente'/cor",
       not any(k in m["caption"].lower() for m in _midias
               for k in ("de tras", "de trás", "costas", "frente", "verso")))
    ok("B10 toda legenda tem o marcador de cod",
       all("_cód:" in m["caption"] for m in _midias))
    ok("B11 nenhuma legenda vazia", all(m["caption"].strip() for m in _midias))

    antes = list(urls())
    n2 = bot._enviar_fotos_extras_pendentes(uid, pend)   # 2a chamada do sender
    ok("B12 2o sender envia 0 (janela fechada)", n2 == 0 and urls() == antes, f"n2={n2}")

    # ... e mesmo com um coletor NOVO com o mesmo pid, o registro de vistas barra
    pend2 = bot._FotosPendentes()
    bot.criar_tool_mostrar_fotos(uid, pend2)(PID_5)      # 5 - 4 = 1 nova
    n3 = bot._enviar_fotos_extras_pendentes(uid, pend2)
    ok("B13 coletor novo nao reenvia foto ja enviada",
       n3 == 1 and len(set(urls())) == len(urls()), f"n3={n3} urls={len(urls())}")


# --------------------------------------------------------------------------- C
def teste_c():
    print("\n### C. paginacao entre turnos")
    uid = "t_fotos_c"
    reset(uid)
    # turno 1: pede as 4 primeiras
    p1 = bot._FotosPendentes()
    r1 = bot.criar_tool_mostrar_fotos(uid, p1)(PID_5)
    bot._enviar_fotos_extras_pendentes(uid, p1)
    ok("C1 turno1 vai_enviar 4", r1["vai_enviar"] == 4 and r1["restantes"] == 1, str(r1))
    # turno 2: sobra 1
    p2 = bot._FotosPendentes()
    r2 = bot.criar_tool_mostrar_fotos(uid, p2)(PID_5)
    ok("C2 turno2 ok/vai_enviar 1", r2["status"] == "ok" and r2["vai_enviar"] == 1, str(r2))
    ok("C3 ja_vistas 4", r2["ja_vistas"] == 4, r2["ja_vistas"])
    n2 = bot._enviar_fotos_extras_pendentes(uid, p2)
    ok("C4 5a foto enviada", n2 == 1 and len(set(urls())) == 5, f"n2={n2} urls={len(set(urls()))}")
    # turno 3: nada novo
    p3 = bot._FotosPendentes()
    r3 = bot.criar_tool_mostrar_fotos(uid, p3)(PID_5)
    ok("C5 turno3 sem_novas", r3["status"] == "sem_novas", r3["status"])
    ok("C6 n_fotos 5 no sem_novas", r3["n_fotos"] == 5, r3["n_fotos"])
    ok("C7 sem_novas nao agenda envio", len(p3) == 0)
    ok("C8 sem_novas nao promete", r3["vai_enviar"] == 0)


# --------------------------------------------------------------------------- D
def teste_d():
    print("\n### D. a foto do CARD nao volta como extra")
    uid = "t_fotos_d"
    reset(uid)
    fotos = bot._fotos_do_produto(PID_5)
    cache = {int(PID_5): {"nome": "PECA TESTE", "preco": 49.9, "imagem": fotos[0]}}
    bot.renderizar_mensagem_estruturada(uid, "olha essa", [int(PID_5)], cache)
    ok("D1 card saiu como midia", len(_midias) == 1, f"midias={len(_midias)}")
    p = bot._FotosPendentes()
    r = bot.criar_tool_mostrar_fotos(uid, p)(PID_5)
    ok("D2 ja_vistas 1 apos o card", r["ja_vistas"] == 1, str(r))
    ok("D3 vai_enviar 4 (as 4 restantes)", r["vai_enviar"] == 4, r["vai_enviar"])
    bot._enviar_fotos_extras_pendentes(uid, p)
    ok("D4 5 midias no total", len(_midias) == 5, len(_midias))
    ok("D5 5 URLs DISTINTAS (card + 4 extras)",
       len(set(urls())) == 5, f"{len(set(urls()))} distintas de {len(urls())}")
    ok("D6 a foto do card nao repetiu", urls().count(fotos[0]) == 1)


# --------------------------------------------------------------------------- E
def teste_e():
    print("\n### E. produto de 1 foto (bucket de 216 produtos)")
    uid = "t_fotos_e"
    reset(uid)
    p = bot._FotosPendentes()
    r = bot.criar_tool_mostrar_fotos(uid, p)(PID_1)
    ok("E1 ok com n_fotos 1", r["status"] == "ok" and r["n_fotos"] == 1, str(r))
    ok("E2 promete 1", r["vai_enviar"] == 1 and r["restantes"] == 0)
    n = bot._enviar_fotos_extras_pendentes(uid, p)
    ok("E3 enviou exatamente 1", n == 1 and len(_midias) == 1, f"n={n}")
    # produto de 1 foto NAO tem "outro angulo": a legenda nao pode mentir
    cap = _midias[0]["caption"].lower()
    ok("E3b legenda nao promete outro angulo", "outro ângulo" not in cap
       and "outros ângulos" not in cap, _midias[0]["caption"][:48])
    ok("E3c legenda diz que e a unica", "única foto" in cap, _midias[0]["caption"][:48])
    p2 = bot._FotosPendentes()
    r2 = bot.criar_tool_mostrar_fotos(uid, p2)(PID_1)
    ok("E4 2o pedido: sem_novas", r2["status"] == "sem_novas", r2["status"])
    # e o caminho do card: se o cliente ja viu o card, o 1o pedido ja e sem_novas
    reset("t_fotos_e2")
    fotos = bot._fotos_do_produto(PID_1)
    bot.renderizar_mensagem_estruturada("t_fotos_e2", "olha", [int(PID_1)],
                                        {int(PID_1): {"nome": "X", "preco": 1.0, "imagem": fotos[0]}})
    r3 = bot.criar_tool_mostrar_fotos("t_fotos_e2", bot._FotosPendentes())(PID_1)
    ok("E5 1 foto ja vista no card -> sem_novas (oferece atendente)",
       r3["status"] == "sem_novas" and r3["ja_vistas"] == 1, str(r3))


# --------------------------------------------------------------------------- F
def teste_f():
    print("\n### F. chave pid consistente entre gravacao e leitura")
    uid = "t_fotos_f"
    reset(uid)
    fotos = bot._fotos_do_produto(PID_5)
    bot._marcar_fotos_vistas(uid, int(PID_5), [fotos[0]])       # grava com int
    ok("F1 le com str", bot._fotos_ja_vistas(uid, PID_5) == [fotos[0]])
    ok("F2 le com float", bot._fotos_ja_vistas(uid, float(PID_5)) == [fotos[0]])
    r = bot.criar_tool_mostrar_fotos(uid, bot._FotosPendentes())(PID_5)
    ok("F3 tool ve ja_vistas 1", r["ja_vistas"] == 1, str(r))
    _f, _v, novas = bot._fotos_novas(uid, PID_5)
    ok("F4 novas = 4", len(novas) == 4 and fotos[0] not in novas)


# --------------------------------------------------------------------------- G
def teste_g():
    print("\n### G. um produto por turno")
    uid = "t_fotos_g"
    reset(uid)
    p = bot._FotosPendentes()
    tool = bot.criar_tool_mostrar_fotos(uid, p)
    r1 = tool(PID_5)
    r2 = tool(PID_10)
    ok("G1 1o ok", r1["status"] == "ok")
    ok("G2 2o limite_turno", r2["status"] == "limite_turno", r2["status"])
    ok("G3 2o nao promete envio", r2["vai_enviar"] == 0)
    ok("G4 so 1 pid no coletor", len(p) == 1)
    n = bot._enviar_fotos_extras_pendentes(uid, p)
    ok("G5 sender respeita o teto do turno", n == bot.FOTOS_MAX_POR_TURNO, f"n={n}")
    ok("G6 nada do 2o produto foi enviado",
       set(urls()).issubset(set(bot._fotos_do_produto(PID_5))))


# --------------------------------------------------------------------------- H
def teste_h():
    print("\n### H. a tool roda DENTRO da thread de trabalho do _ia_send_com_teto")
    uid = "t_fotos_h"
    reset(uid)
    p = bot._FotosPendentes()
    tool = bot.criar_tool_mostrar_fotos(uid, p)
    caixa = {}

    class _ChatFake:
        """Faz o que o SDK faz: executa a tool na thread de trabalho."""
        def send_message(self, texto, request_options=None):
            caixa["ctx_user"] = bot._ctx_user_id()
            caixa["thread"] = threading.current_thread().name
            caixa["r"] = tool(PID_5)
            return "resposta"

    bot._set_turn_ctx(uid, "manda mais fotos")
    try:
        out = bot._ia_send_com_teto(_ChatFake(), "manda mais fotos", 10.0)
    finally:
        bot._clear_turn_ctx()
    ok("H1 send_message retornou", out == "resposta")
    ok("H2 rodou na thread 'ia-send'", caixa.get("thread") == "ia-send", caixa.get("thread"))
    ok("H3 contexto do turno reinstalado la", caixa.get("ctx_user") == uid, caixa.get("ctx_user"))
    ok("H4 tool funcionou na thread de trabalho", caixa["r"]["status"] == "ok", str(caixa.get("r")))
    ok("H5 pid coletado na thread e visivel para quem espera",
       len(p) == 1 and PID_5 in p, f"len={len(p)}")
    n = bot._enviar_fotos_extras_pendentes(uid, p)
    ok("H6 sender (thread principal) enviou o que a tool prometeu", n == 4, f"n={n}")


# --------------------------------------------------------------------------- I
def teste_i():
    print("\n### I. thread ABANDONADA nao envia foto de turno encerrado")
    uid = "t_fotos_i"
    reset(uid)
    p = bot._FotosPendentes()
    tool = bot.criar_tool_mostrar_fotos(uid, p)
    tool(PID_5)
    n1 = bot._enviar_fotos_extras_pendentes(uid, p)     # turno termina aqui
    ok("I1 turno enviou 4", n1 == 4, f"n1={n1}")
    # a thread zumbi acorda DEPOIS e chama a tool de novo (as tools dela ja rodaram)
    r = tool(PID_5)
    ok("I2 tool nao promete envio com a janela fechada",
       r["vai_enviar"] == 0, str(r)[:110])
    ok("I3 janela fechada nao aceita pid", len(p) == 0)
    antes = list(urls())
    n2 = bot._enviar_fotos_extras_pendentes(uid, p)
    ok("I4 nenhum envio extra", n2 == 0 and urls() == antes, f"n2={n2}")
    # 2a defesa, independente do coletor: o registro de vistas
    p2 = bot._FotosPendentes()
    bot.criar_tool_mostrar_fotos(uid, p2)(PID_5)
    n3 = bot._enviar_fotos_extras_pendentes(uid, p2)
    ok("I5 nenhuma URL repetida no total", len(set(urls())) == len(urls()), f"urls={urls().__len__()}")
    ok("I6 so a 5a foto (nova) saiu", n3 == 1)


# --------------------------------------------------------------------------- J
def teste_j():
    print("\n### J. sem foto nenhuma + turno que falha")
    uid = "t_fotos_j"
    reset(uid)
    p = bot._FotosPendentes()
    r = bot.criar_tool_mostrar_fotos(uid, p)(PID_0)
    ok("J1 sem_foto", r["status"] == "sem_foto" and r["n_fotos"] == 0, str(r)[:90])
    ok("J2 nao agenda envio", len(p) == 0)
    ok("J3 sender no-op", bot._enviar_fotos_extras_pendentes(uid, p) == 0)
    ok("J4 id invalido -> erro", bot.criar_tool_mostrar_fotos(uid, p)("xyz")["status"] == "erro")

    # turno que levanta: os pedidos de foto sao DESCARTADOS (nao ha anuncio ao cliente)
    reset(uid)
    p2 = bot._FotosPendentes()
    bot.criar_tool_mostrar_fotos(uid, p2)(PID_5)
    _orig = bot.get_history
    bot.get_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falha simulada"))
    try:
        bot._executar_turno(uid, "manda mais fotos", p2)
    finally:
        bot.get_history = _orig
    ok("J5 turno com erro fecha a janela", len(p2) == 0)
    n = bot._enviar_fotos_extras_pendentes(uid, p2)
    ok("J6 nenhuma foto sai de turno que falhou", n == 0, f"n={n}")


_UIDS = ["t_fotos_b", "t_fotos_c", "t_fotos_d", "t_fotos_e", "t_fotos_e2",
         "t_fotos_f", "t_fotos_g", "t_fotos_h", "t_fotos_i", "t_fotos_j"]


def _cleanup():
    """J chama `_executar_turno`, que grava em chat_history/bot_turns (banco de
    PRODUCAO). Sem isto o teste deixa lixo."""
    print("\nLimpando rows de teste (chat_history/bot_turns/tool_filtro_eventos)...")
    for uid in _UIDS:
        for tbl in ("chat_history", "bot_turns", "tool_filtro_eventos"):
            try:
                bot.supabase.table(tbl).delete().eq("user_id", uid).execute()
            except Exception as e:
                print(f"  cleanup {tbl} uid={uid}: {str(e)[:90]}")


def main():
    print(f"FOTOS_SOB_DEMANDA={bot.FOTOS_SOB_DEMANDA} "
          f"FOTOS_MAX_POR_PEDIDO={bot.FOTOS_MAX_POR_PEDIDO} "
          f"FOTOS_MAX_POR_TURNO={bot.FOTOS_MAX_POR_TURNO} TTL={bot.FOTOS_TTL_SEG}s")
    try:
        for fn in (teste_a, teste_b, teste_c, teste_d, teste_e,
                   teste_f, teste_g, teste_h, teste_i, teste_j):
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                ok(f"{fn.__name__} CRASHOU", False, str(e)[:120])
    finally:
        _cleanup()
    print(f"\n{'=' * 60}\nRESULTADO: pass={RES['pass']} fail={RES['fail']}")
    return 1 if RES["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
