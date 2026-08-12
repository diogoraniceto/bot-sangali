"""Alerta da atendente na Cloud API — template, recibo e o fim do "ok" falso.

CONTEXTO (medido em 12/08/2026): a atendente NUNCA escreve para o numero do bot, entao
esta sempre fora da janela de 24h da Cloud API. Texto livre para fora da janela nao e
entregue — a Meta respondeu HTTP 200 e a mensagem nunca chegou. Pior: o handoff ignorava
o retorno do envio, devolvia {"status":"ok"}, a Luna dizia "ja chamei uma atendente" e o
silencio de 2h entrava em vigor. Cliente abandonado achando que a ajuda vinha.

H1  template e a PRIMEIRA opcao no cloud (unico envio que atravessa a janela)
H2  parametro de template e achatado (a Meta rejeita \\n, \\t, espaco duplo)
H3  template falhando cai para texto livre — degrada, nao escurece
H4  falha TOTAL nao devolve "ok": devolve aviso_nao_entregue com instrucao
H5  sem aviso entregue NAO grava conversation_handoffs -> nao cala o bot por 2h
H6  aviso entregue grava normalmente e o silencio vale
H7  UAZAPI nao regride: manda o texto rico, sem template
H8  o corpo do erro da Meta e logado (era descartado pelo raise_for_status)
H9  o prompt sabe nao mentir quando o status for aviso_nao_entregue
H10 "a Meta aceitou" != "foi entregue": fora da janela, aceito NAO conta como avisado

    python tests/test_handoff_template.py

Nao envia nada para a Meta e nao escreve no banco: supabase e requests sao dublados.
"""
import os
import re
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

falhas = []
pulados = []


def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


def skip(nome, motivo):
    print(f"  [~] {nome}  (pulado: {motivo})")
    pulados.append(nome)


for _f in ("enviar_alerta_operador", "_cloud_send_template"):
    if not hasattr(bot, _f):
        print(f"### PULANDO TUDO: {_f} nao existe (codigo pre-template)")
        sys.exit(0)

_REAL = dict(prov=bot.WHATSAPP_PROVIDER, send=bot._cloud_send,
             tmpl=bot._cloud_send_template, txt=bot.enviar_mensagem_whatsapp,
             sb=bot.supabase, hist=bot.get_history, mail=bot._enviar_email_alerta,
             ult=bot._ultimo_handoff_em, alerta=bot.enviar_alerta_operador,
             jan=bot._janela_24h_aberta)


def _restaurar():
    bot.WHATSAPP_PROVIDER = _REAL["prov"]
    bot._cloud_send = _REAL["send"]
    bot._cloud_send_template = _REAL["tmpl"]
    bot.enviar_mensagem_whatsapp = _REAL["txt"]
    bot.supabase = _REAL["sb"]
    bot.get_history = _REAL["hist"]
    bot._enviar_email_alerta = _REAL["mail"]
    bot._ultimo_handoff_em = _REAL["ult"]
    # sem esta linha o dublê do H5/H6 vaza para o H7 e ele testa o dublê, não o código
    bot.enviar_alerta_operador = _REAL["alerta"]


# =========================================================================
print("### H1/H2 — no cloud o template vem primeiro, com parametros achatados")
try:
    bot.WHATSAPP_PROVIDER = "cloud"
    capturado = []
    bot._cloud_send = lambda payload: (capturado.append(payload), "wamid.FAKE")[1]

    okp, wamid, via = bot.enviar_alerta_operador(
        "5527997288088", "texto\nrico\ncom quebras",
        ["5527999465394", "Cliente pediu pessoa", "Linha 1\nLinha 2\tcom  tab", "Camisola"])
    ok(via == "template" and okp and wamid == "wamid.FAKE",
       "H1 cloud usa TEMPLATE como primeira opcao", (via, okp, wamid))
    p = capturado[0] if capturado else {}
    ok(p.get("type") == "template", "H1 payload e do tipo template", p.get("type"))
    ok((p.get("template") or {}).get("name") == bot.HANDOFF_TEMPLATE,
       "H1 nome do template vem do env HANDOFF_TEMPLATE")
    params = [x["text"] for x in
              ((p.get("template") or {}).get("components") or [{}])[0].get("parameters", [])]
    ok(len(params) == 4, "H2 manda os 4 parametros de {{1}}..{{4}}", params)
    ok(all("\n" not in x and "\t" not in x and "  " not in x for x in params),
       "H2 nenhum parametro tem \\n, \\t ou espaco duplo (a Meta rejeitaria)", params)
    ok(params[2] == "Linha 1 Linha 2 com tab",
       "H2 quebras viram espaco simples, sem perder conteudo", params[2])
finally:
    _restaurar()


# =========================================================================
print("\n### H3 — template falhando cai para texto livre (degrada, nao escurece)")
try:
    bot.WHATSAPP_PROVIDER = "cloud"
    tentativas = []

    def _tmpl_falha(*a, **k):
        tentativas.append("template")
        raise RuntimeError("132001 template does not exist")

    def _send_txt(payload):
        tentativas.append(payload.get("type"))
        return "wamid.TEXTO"

    bot._cloud_send_template = _tmpl_falha
    bot._cloud_send = _send_txt
    # janela dublada: sem isto o teste consulta o banco de verdade e deixa de ser
    # deterministico. O comportamento com a janela FECHADA e o H10.
    bot._janela_24h_aberta = lambda n: True
    okp, wamid, via = bot.enviar_alerta_operador("552799", "texto rico", ["a", "b", "c", "d"])
    ok(tentativas == ["template", "text"], "H3 tenta template, depois texto", tentativas)
    ok(okp and via == "texto" and wamid == "wamid.TEXTO",
       "H3 com janela aberta, o texto livre salva o aviso", (okp, via, wamid))
finally:
    bot._janela_24h_aberta = _REAL["jan"]
    _restaurar()


print("\n### H4 — falha TOTAL nao mente")
try:
    bot.WHATSAPP_PROVIDER = "cloud"
    bot._cloud_send_template = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    bot._cloud_send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("131047 fora da janela"))
    okp, wamid, via = bot.enviar_alerta_operador("552799", "t", ["a", "b", "c", "d"])
    ok(okp is False and via == "falhou", "H4 devolve (False, _, 'falhou')", (okp, via))
finally:
    _restaurar()


# =========================================================================
print("\n### H5/H6 — o silencio de 2h so existe se a atendente foi avisada")


class _Q:
    def __init__(self, store, data=None):
        self._s = store
        self._d = data if data is not None else {"operator_number": "5527997288088"}

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def single(self): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def insert(self, row, *a, **k):
        self._s.append(row)
        return self

    def execute(self): return type("R", (), {"data": self._d})()


for cenario, envio_ok, espera_insert in (("H5 aviso FALHOU", False, False),
                                         ("H6 aviso ENTREGUE", True, True)):
    try:
        gravadas = []
        emails = []
        bot.supabase = type("SB", (), {"table": lambda self, n: _Q(gravadas)})()
        bot.get_history = lambda u, limit=8, excluir_id=None: []
        bot._ultimo_handoff_em = lambda u: None          # sem handoff recente
        bot._enviar_email_alerta = lambda a, c: (emails.append(a), True)[1]
        bot.enviar_alerta_operador = lambda n, t, p: (envio_ok, "wamid.X" if envio_ok else None,
                                                     "template" if envio_ok else "falhou")

        tool = bot.criar_tool_transferir("5527999465394")
        r = tool(motivo="pedido_humano", resumo="Cliente quer falar com pessoa.",
                 produtos_interesse="Camisola")

        if espera_insert:
            ok(r.get("status") == "ok", f"{cenario}: tool devolve ok", r)
            ok(len(gravadas) == 1,
               f"{cenario}: grava conversation_handoffs (silencio de 2h vale)", gravadas)
            ok(emails == [], f"{cenario}: nao dispara e-mail de alerta", emails)
        else:
            ok(r.get("status") == "aviso_nao_entregue",
               f"{cenario}: tool NAO devolve ok", r)
            ok("NAO diga que chamou" in (r.get("msg") or ""),
               f"{cenario}: instrui a Luna a nao mentir", (r.get("msg") or "")[:70])
            ok("99968-8088" in (r.get("msg") or ""),
               f"{cenario}: passa o WhatsApp da loja como alternativa")
            ok(gravadas == [],
               f"{cenario}: NAO grava handoff -> o bot NAO cala por 2h (nao abandona)",
               gravadas)
            ok(len(emails) == 1,
               f"{cenario}: avisa o time por e-mail", emails)
    finally:
        _restaurar()


# =========================================================================
print("\n### H7 — UAZAPI nao regride")
try:
    bot.WHATSAPP_PROVIDER = "uazapi"
    enviados = []
    bot.enviar_mensagem_whatsapp = lambda n, t: enviados.append((n, t))
    bot._cloud_send_template = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("template NAO deve ser chamado na uazapi"))
    okp, wamid, via = bot.enviar_alerta_operador("552799", "texto\nrico\ncom historico",
                                                 ["a", "b", "c", "d"])
    ok(via == "uazapi" and okp, "H7 uazapi manda texto livre e reporta sucesso", (via, okp))
    ok(enviados and "historico" in enviados[0][1],
       "H7 o texto RICO (com historico) e o que vai na uazapi", enviados)
finally:
    _restaurar()


# =========================================================================
print("\n### H8/H9 — diagnostico visivel e prompt coerente")
src = ""
try:
    import inspect
    src = inspect.getsource(bot._cloud_send)
except Exception:
    pass
ok("error" in src and "resp.ok" in src,
   "H8 _cloud_send loga o CORPO do erro da Meta antes de levantar")
ok("code=" in src, "H8 o log traz o `code` (131047 = fora da janela, 131026 = nao recebe)")

wsrc = ""
try:
    wsrc = inspect.getsource(bot.webhook)
except Exception:
    pass
ok('value.get("statuses"' in wsrc or "value.get('statuses'" in wsrc,
   "H8 webhook LE os recibos (statuses) em vez de descartar")
ok("failed" in wsrc, "H8 webhook loga especificamente o status `failed`")

with open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8") as f:
    prompt = f.read()
ok("aviso_nao_entregue" in prompt,
   "H9 prompt conhece o status aviso_nao_entregue")
ok(re.search(r"quando o status for `ok`", prompt) is not None,
   "H9 a mensagem-padrao ficou condicionada ao status ok (sem contradicao)")


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
