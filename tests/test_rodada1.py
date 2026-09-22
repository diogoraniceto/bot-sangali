"""Rodada 1 do PLANO_CAMPANHA_2 — A1 (abertura de atacado), A4 (friccao), A5 (abuso).

Tudo aqui e deterministico e roda SEM Gemini: testa a dica dinamica, o ramo
silencioso do handoff (com stubs) e o contrato do prompt. O comportamento do
modelo com essas mudancas e medido no gate, nao aqui.

R1  A1: `_dica_abertura_atacado` dispara quando deve e so quando deve
R2  A1: o texto da dica aponta para o §9.0 e para modo_preco atacado_avista
R3  A5: motivo `encerrado_abuso` grava o handoff e NAO chama alerta nem e-mail
R4  A5: motivo normal continua alertando (nao regredimos o caminho que funciona)
R5  A5: knob de env desliga o silencio sem deploy
R6  contrato do prompt: §2, §3, §9.0, §15 tem o que a rodada prometeu
R7  prompt local == banco (a defasagem ja custou uma rodada de gate)

    python tests/test_rodada1.py
"""
import os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ["ENABLE_INPROCESS_SYNC"] = "0"
os.environ.setdefault("WHATSAPP_PROVIDER", "uazapi")
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))
import bot  # noqa: E402

falhas, pulados = [], []
def ok(c, nome, extra=""):
    print(("  [+] " if c else "  [-] ") + nome + ("" if c else f"  <-- {extra}"))
    if not c: falhas.append(nome)
def skip(nome, motivo):
    print(f"  [~] {nome}  (pulado: {motivo})"); pulados.append(nome)

# ------------------------------------------------------------------ R1 / R2
print("### R1 — A1: quando a dica de abertura de atacado entra")
D = bot._dica_abertura_atacado
for msg in ("Vocês vendem atacado?", "quero revender lingerie", "vcs trabalham com atacado",
            "[transcrição de áudio do cliente:] oi, vocês vendem no atacado?",
            "Sou de Guaçuí, revenda Joici"):
    ok(D(msg, []), f"R1 dispara: {msg[:44]!r}")
for msg in ("oi", "queria ver camisolas tamanho G", "tem baby doll?", "quanto custa",
            "[transcrição de áudio do cliente:] bom dia"):
    ok(not D(msg, []), f"R1 NAO dispara: {msg[:44]!r}")
ok(not D("Vocês vendem atacado?", ["8147660"]),
   "R1 NAO dispara se o cliente JA viu card (a dica some sozinha)")
ok(not D("", []) and not D(None, []), "R1 texto vazio/None nao dispara nem quebra")

print("\n### R2 — A1: o que a dica manda fazer")
d = bot._DICA_ABERTURA_ATACADO
ok(d.startswith("[ABERTURA DE ATACADO"), "R2 marcacao que o §9.0 reconhece")
ok("9.0" in d, "R2 aponta para o §9.0")
ok("atacado_avista" in d, "R2 pede modo_preco atacado_avista")
ok("SEM tamanho" in d, "R2 manda buscar SEM tamanho (revendedor compra grade)")
ok("DEPOIS dos cards" in d, "R2 minimo depois dos cards, nao antes")
ok("categoria que ele pediu" in d, "R2 respeita a categoria que o cliente nomeou")

# ------------------------------------------------------------------ R3 / R4 / R5
print("\n### R3/R4 — A5: handoff silencioso vs handoff normal (stubs)")
_REAL = dict(supabase=bot.supabase, alerta=bot.enviar_alerta_operador,
             email=bot._enviar_email_alerta, ultimo=bot._ultimo_handoff_em,
             hist=bot.get_history, silenciosos=set(bot.MOTIVOS_SILENCIOSOS))
gravados, alertas, emails = [], [], []

class _Exec:
    def __init__(self, data): self.data = data
    def execute(self): return self
class _Tbl:
    def __init__(self, nome): self.nome = nome
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def single(self): return _Exec({"operator_number": "5527999999999"})
    def insert(self, row): gravados.append((self.nome, row)); return _Exec([row])
class _SB:
    def table(self, n): return _Tbl(n)

try:
    bot.supabase = _SB()
    bot._ultimo_handoff_em = lambda u: None            # sem handoff recente
    bot.get_history = lambda u, limit=8, excluir_id=None: []   # _montar_mensagem_operador le o historico
    # assinatura real: (ok, wamid, via) — 3 valores
    bot.enviar_alerta_operador = lambda num, txt, params: (alertas.append((num, txt)), (True, "wamid-x", "template"))[1]
    bot._enviar_email_alerta = lambda *a, **k: emails.append(a)

    tool = bot.criar_tool_transferir("test_r1_abuso")
    r = tool(motivo="encerrado_abuso", resumo="cliente mandou foto de genital", produtos_interesse="")
    ok(r.get("status") == "ok", "R3 encerrado_abuso devolve status ok (modelo manda a msg-padrao e para)", r)
    ok(len(gravados) == 1 and gravados[0][0] == "conversation_handoffs",
       "R3 gravou em conversation_handoffs (ativa o silencio de 2h)", gravados)
    ok(gravados and gravados[0][1].get("motivo") == "encerrado_abuso", "R3 motivo gravado e o certo")
    ok(alertas == [], "R3 NAO chamou enviar_alerta_operador (atendente nao acordou)", alertas)
    ok(emails == [], "R3 NAO disparou e-mail", emails)
    ok("mais ninguem" in (r.get("msg") or ""), "R3 msg orienta o modelo a nao chamar mais ninguem")

    gravados.clear(); alertas.clear(); emails.clear()
    r2 = tool(motivo="pedido_humano", resumo="cliente pediu vendedor", produtos_interesse="")
    ok(len(alertas) == 1, "R4 pedido_humano CONTINUA alertando a atendente", alertas)
    ok(len(gravados) == 1, "R4 ... e gravando o handoff", gravados)
    ok(r2.get("status") == "ok", "R4 ... com status ok", r2)

    print("\n### R5 — A5: knob HANDOFF_MOTIVOS_SILENCIOSOS")
    gravados.clear(); alertas.clear()
    bot.MOTIVOS_SILENCIOSOS = set()
    r3 = tool(motivo="encerrado_abuso", resumo="x", produtos_interesse="")
    ok(len(alertas) == 1, "R5 com o set vazio, encerrado_abuso volta a alertar (desliga sem deploy)", alertas)
    ok("encerrado_abuso" in bot._MOTIVO_LABEL, "R5 label existe para o caso de alertar")
    ok("ninguém foi avisado" in bot._MOTIVO_LABEL["encerrado_abuso"] or "ninguem" in bot._MOTIVO_LABEL["encerrado_abuso"].lower(),
       "R5 ... e o label deixa claro que e registro, nao pedido de acao")
finally:
    bot.supabase = _REAL["supabase"]; bot.enviar_alerta_operador = _REAL["alerta"]
    bot._enviar_email_alerta = _REAL["email"]; bot._ultimo_handoff_em = _REAL["ultimo"]
    bot.get_history = _REAL["hist"]
    bot.MOTIVOS_SILENCIOSOS = _REAL["silenciosos"]
ok("encerrado_abuso" in bot.MOTIVOS_SILENCIOSOS, "R5 default restaurado: encerrado_abuso e silencioso")

# ------------------------------------------------------------------ R6
print("\n### R6 — contrato do prompt")
with open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8") as f:
    P = f.read()
ok("busque SEM tamanho e MOSTRE" in P, "R6 A4 §2: produto primeiro, tamanho junto")
ok("NÃO peça o nome na primeira resposta" in P, "R6 A4 §3: sem 'com quem eu falo?' na abertura")
ok("Nunca pergunte tamanho antes de mostrar em ATACADO" in P, "R6 A1/A4 §2: atacado nao tem porta de tamanho")
ok("## 9.0. ABERTURA DE ATACADO" in P, "R6 A1 §9.0 existe")
ok("[ABERTURA DE ATACADO" in P, "R6 A1 §9.0 reconhece a marcacao dinamica")
ok("Condições de pagamento, parcelas, política de troca: SÓ se ele perguntar" in P,
   "R6 A1 §9.0: regras so se perguntar")
ok("em 6 casos" in P and "um dos 6 acima" in P and "fora desses 6" in P,
   "R6 A5 §15: contagem 5 -> 6 em TODOS os lugares (o modelo le os tres)")
ok("| 6 | `encerrado_abuso` |" in P, "R6 A5 §15: linha do caso 6")
ok("NÃO acorda a atendente" in P, "R6 A5 §15: caso 6 diz que nao acorda humano")
ok("NUNCA é fechamento de venda" in P, "R6 A5 §15: assedio nunca vira fechamento_venda")
ok("VOCÊ NÃO CALCULA TOTAL DE CABEÇA. NUNCA." in P, "R6 §9 original preservado abaixo do 9.0")
# Achado na aceitacao de 22/09: A1 mostrou os 3 conjuntos certos mas com modo_preco
# 'varejo'. O §0 dizia "atacado_avista quando esta comprando a vista (pix)" e "em
# duvida, varejo" — para um lead que so perguntou "vendem atacado?" isso e duvida,
# e o §0 (contrato, lido primeiro) vencia o §9.0. A duvida legitima e a vista x
# prazo, nunca atacado x varejo.
ok("JÁ NA PRIMEIRA resposta com cards, mesmo que ele ainda não tenha dito como vai pagar" in P,
   "R6 A1 §0: modo_preco atacado ja na 1a resposta, sem esperar forma de pagamento")
ok("Em dúvida, use `\"varejo\"`" not in P,
   "R6 A1 §0: a cautela 'em duvida, varejo' SAIU (era o que derrubava o §9.0)")
ok("NUNCA resolva \"atacado ou varejo?\" a favor do varejo" in P,
   "R6 A1 §0: a duvida legitima e a vista x prazo, nao atacado x varejo")
ok("Se for LINGERIE/PIJAMA e faltar tamanho → perguntar." not in P,
   "R6 A4: a regra antiga de porta de tamanho SAIU (nao pode coexistir)")

# ------------------------------------------------------------------ R7
print("\n### R7 — prompt local == banco")
try:
    db = bot.supabase.table("bot_settings").select("system_prompt").eq("id", 1).single().execute()
    dbp = (db.data or {}).get("system_prompt")
    if dbp is None: skip("R7", "sem acesso a bot_settings")
    else: ok(dbp == P, "R7 prompt_luna_v2.txt byte-identico ao banco (senao o gate mede outra coisa)",
             f"local={len(P)} banco={len(dbp)}")
except Exception as e:
    skip("R7", f"{type(e).__name__}: {str(e)[:60]}")

print("\n" + "=" * 60)
if pulados: print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas: print(f"FALHAS ({len(falhas)}): {falhas}"); sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
