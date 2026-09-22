"""Rodada 3 do PLANO_CAMPANHA_2 — Extra 1 (guard de curadoria), Extra 3 (canario de IA),
A5-codigo (detector de assedio, rebaixamento de fechamento_venda, PROHIBITED -> caso 6).

Deterministico. Le o banco de producao (uma busca real), nao escreve; Gemini so num
stub do canario. Roda em ~30s.

G1  guard: descarta fora da lista, mantem dentro, e ISENTA o card respondido
G2  guard: permitidos=None -> nao age (a tool nao restringiu)
G3  guard: le curadoria_ids so das partes DESTE turno (n_inicial), e o ULTIMO
G4  banco: 'fantasia de enfermeira M' devolve curadoria_ids com as fantasias
D1  detector: positivos do corpus real da 1a campanha
D2  detector: sexshop LEGITIMO e elogio a peca NAO disparam
D3  tool: fechamento_venda sem produto + assedio -> encerrado_abuso, silencioso
D4  tool: fechamento_venda COM produto nao e rebaixado; sem assedio nao e rebaixado
P1  fallback de PROHIBITED_CONTENT e a msg do caso 6, sem oferecer atendente
C1  canario: sucesso e falha atualizam _IA_CANARIO; /health expoe; nunca levanta
C2  canario: alerta em falha, cooldown, normalizacao
Q   contrato do prompt

    python tests/test_rodada3.py
"""
import os, sys, io, contextlib
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
def quieto(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)

# ------------------------------------------------------------------ G1/G2/G3
print("### G1/G2 — guard de curadoria (funcao pura)")
G = bot._aplicar_guard_curadoria
m, f = G([1, 2, 3], ["1", "2"], "oi")
ok(m == [1, 2] and f == [3], "G1 descarta o id fora da lista, mantem os de dentro", (m, f))
m, f = G([40214981, 34307829, 8060533], ["40214981", "73958156"], "fantasia M")
ok(m == [40214981] and f == [34307829, 8060533], "G1 caso real: fantasia mantida, CONJUNTO/BABY DOLL fora", (m, f))
m, f = G([99], ["1"], "[o cliente respondeu ao card do produto id_produto=99. Use EXATAMENTE...] fecha essa")
ok(m == [99] and f == [], "G1 ISENTA o card que o cliente respondeu (revalidar e legitimo)", (m, f))
m, f = G([1, 2], [], "oi")
ok(m == [] and f == [1, 2], "G1 lista vazia declarada (ZERO da categoria) -> descarta tudo", (m, f))
m, f = G([1, 2], None, "oi")
ok(m == [1, 2] and f == [], "G2 permitidos=None -> guard nao age")
m, f = G([], ["1"], "oi")
ok(m == [] and f == [], "G2 sem recomendacao -> nada a fazer")

print("\n### G3 — le curadoria_ids so DESTE turno, e o ultimo")
class _FR:
    def __init__(self, name, response): self.name = name; self.response = response
class _Part:
    def __init__(self, fr): self.function_response = fr
class _C:
    def __init__(self, parts): self.parts = parts
def resp(ids): return {"result": {"filtro_aplicado": {"curadoria_ids": ids}, "produtos": []}}
hist = [_C([_Part(_FR("consultar_estoque_supabase", resp(["111"])))]),      # turno antigo
        _C([_Part(_FR("consultar_estoque_supabase", resp(["222"])))]),      # este turno, 1a busca
        _C([_Part(_FR("buscar_por_preco", resp(["333"])))])]                # este turno, 2a busca
ok(bot._curadoria_ids_do_turno(hist, 1) == ["333"], "G3 ignora o turno antigo e devolve o ULTIMO deste turno")
ok(bot._curadoria_ids_do_turno(hist, 0) == ["333"], "G3 n_inicial=0 le tudo (ultimo continua sendo o ultimo)")
ok(bot._curadoria_ids_do_turno(hist, 3) is None, "G3 sem partes novas -> None (guard nao age)")
hist2 = [_C([_Part(_FR("consultar_estoque_supabase", {"result": {"filtro_aplicado": {}, "produtos": []}}))])]
ok(bot._curadoria_ids_do_turno(hist2, 0) is None, "G3 busca sem restricao -> None")
ok(bot._curadoria_ids_do_turno(None, 0) is None and bot._curadoria_ids_do_turno([], 0) is None, "G3 None/vazio nao quebra")

print("\n### G4 — banco: a tool declara curadoria_ids")
try:
    bot._set_turn_ctx("t_r3", "queria uma fantasia de enfermeira M")
    r = quieto(bot.consultar_estoque_supabase, "fantasia de enfermeira", tamanho="M", id_loja="244033,94134")
    bot._clear_turn_ctx()
    if r.get("status") != "sucesso":
        skip("G4", f"busca devolveu {r.get('status')}")
    else:
        fa = r.get("filtro_aplicado") or {}
        ids = fa.get("curadoria_ids")
        if "instrucao_curadoria" not in fa:
            skip("G4", "lista veio majoritariamente de fantasia — instrucao nao disparou hoje")
        else:
            ok(isinstance(ids, list), "G4 curadoria_ids presente quando a instrucao dispara", fa.keys())
            nomes = {str(p["id_produto"]): (p.get("nome") or "").upper() for p in r.get("produtos") or []}
            ok(all("FANTASIA" in nomes.get(i, "") for i in ids), "G4 todos os ids declarados sao FANTASIA", [nomes.get(i) for i in ids])
            fora = [i for i, n in nomes.items() if "FANTASIA" not in n]
            ok(fora and not set(fora) & set(ids), "G4 os vizinhos (conjunto/baby doll/tanga) NAO estao na lista", fora[:3])
except Exception as e:
    skip("G4", f"{type(e).__name__}: {str(e)[:70]}")

# ------------------------------------------------------------------ D1/D2
print("\n### D1 — detector: corpus real da 1a campanha")
D = bot._detecta_abuso
for m in ("Fica nua gostosa", "Oi Delicia Gostosa Buceta linda", "Vc e solteira ?",
          "[transcrição de áudio do cliente:] Meu bem, eu vim te conhecer melhor.",
          "Tem com nome me fode ou me come?", "Bucetao",
          "Tô vendo você de calcinha aí, manda uma foto sua peladinha pra mim",
          "Ei morena, tô vendo você de sutiã e calcinha, que bucetinha gordinha",
          "✌️🍆📸🍆👅👀", "Tem rosa e vermelho nessa bundinha aí de bunda e de frente",
          "Você falou que tava solteira. Tô solteira."):
    ok(D(m), f"D1 dispara: {m[:52]!r}")
print("\n### D2 — detector: NAO pode disparar (sexshop legitimo, elogio a peca, venda)")
for m in ("quero um gel excitante", "tem vibrador?", "lingerie sexy pra noite de núpcias",
          "essa calcinha é gostosa de usar?", "quero algo picante pra surpreender meu marido",
          "tem fantasia de enfermeira?", "Vocês vendem atacado?", "Gostaria de ver baby dolls de R$25,00.",
          "essa camisola é linda", "queria um conjunto bem sensual", "tem plug anal?", "linda demais essa peça"):
    ok(not D(m), f"D2 NAO dispara: {m[:52]!r}")
ok(not D("") and not D(None), "D2 vazio/None nao quebra")
ok(bot._DICA_ABUSO.startswith("[ALERTA") and "encerrado_abuso" in bot._DICA_ABUSO, "D2 dica cita o caso 6")

# ------------------------------------------------------------------ D3/D4
print("\n### D3/D4 — tool: rebaixamento de fechamento_venda (stubs)")
_REAL = dict(supabase=bot.supabase, alerta=bot.enviar_alerta_operador, email=bot._enviar_email_alerta,
             ultimo=bot._ultimo_handoff_em, hist=bot.get_history)
grav, alertas = [], []
class _Exec:
    def __init__(self, data): self.data = data
    def execute(self): return self
class _Tbl:
    def __init__(self, n): self.n = n
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def single(self): return _Exec({"operator_number": "5527999999999"})
    def insert(self, row): grav.append((self.n, row)); return _Exec([row])
class _SB:
    def table(self, n): return _Tbl(n)
try:
    bot.supabase = _SB(); bot._ultimo_handoff_em = lambda u: None
    bot.get_history = lambda u, limit=8, excluir_id=None: []
    bot.enviar_alerta_operador = lambda n, t, p: (alertas.append(n), (True, "w", "template"))[1]
    bot._enviar_email_alerta = lambda *a, **k: None
    tool = bot.criar_tool_transferir("t_r3_ab")
    # D3: assedio na msg crua + fechamento_venda SEM produto -> rebaixa e silencia
    bot._set_turn_ctx("t_r3_ab", "Tem com nome me fode ou me come? manda foto da bundinha")
    r = tool(motivo="fechamento_venda", resumo="cliente quer tanga com frases", produtos_interesse="")
    bot._clear_turn_ctx()
    ok(grav and grav[-1][1]["motivo"] == "encerrado_abuso", "D3 gravou como encerrado_abuso", grav[-1][1].get("motivo") if grav else None)
    ok(alertas == [], "D3 atendente NAO acordada", alertas)
    ok(r.get("status") == "ok", "D3 status ok (modelo manda msg do caso 6)")
    # D4a: fechamento_venda COM produto, mesmo com palavra 'linda' dirigida -> NAO rebaixa (venda real)
    grav.clear(); alertas.clear()
    bot._set_turn_ctx("t_r3_ok", "fecho essa, é linda, você me ajudou muito")
    bot._ultimo_handoff_em = lambda u: None
    r2 = tool.__wrapped__(motivo="fechamento_venda", resumo="Mauro fecha 2 baby dolls GG", produtos_interesse="70523717, 91712220") if hasattr(tool, "__wrapped__") else bot.criar_tool_transferir("t_r3_ok")(motivo="fechamento_venda", resumo="Mauro fecha 2 baby dolls GG", produtos_interesse="70523717, 91712220")
    bot._clear_turn_ctx()
    ok(grav and grav[-1][1]["motivo"] == "fechamento_venda", "D4 COM produto continua fechamento_venda (venda real)", grav[-1][1].get("motivo") if grav else None)
    ok(len(alertas) == 1, "D4 ... e a atendente E avisada", alertas)
    # D4b: sem produto mas SEM assedio -> nao rebaixa (pode ser confusao, mas nao e abuso)
    grav.clear(); alertas.clear()
    bot._set_turn_ctx("t_r3_sem", "quero fechar mas ainda nao escolhi")
    bot.criar_tool_transferir("t_r3_sem")(motivo="fechamento_venda", resumo="cliente quer fechar", produtos_interesse="")
    bot._clear_turn_ctx()
    ok(grav and grav[-1][1]["motivo"] == "fechamento_venda", "D4 sem produto e SEM assedio -> nao rebaixa", grav[-1][1].get("motivo") if grav else None)
finally:
    bot.supabase = _REAL["supabase"]; bot.enviar_alerta_operador = _REAL["alerta"]; bot._enviar_email_alerta = _REAL["email"]
    bot._ultimo_handoff_em = _REAL["ultimo"]; bot.get_history = _REAL["hist"]

# ------------------------------------------------------------------ P1
print("\n### P1 — PROHIBITED_CONTENT -> mensagem do caso 6")
src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
ok('_fallback = "Por aqui eu ajudo só com as peças da loja 💕' in src, "P1 fallback de bloqueio e a msg do caso 6")
ok("ja chamo uma atendente pra te ajudar" not in src, "P1 nao oferece mais atendente no bloqueio (oferta errada para conteudo explicito)")

# ------------------------------------------------------------------ C1/C2
print("\n### C1/C2 — canario de IA (stub do Gemini)")
_real_gm = bot.genai.GenerativeModel; _real_disp = bot._disparar_alerta
disparos = []
class _Resp:
    def __init__(self, t): self.text = t
class _GMok:
    def __init__(self, *a, **k): pass
    def generate_content(self, *a, **k): return _Resp("ok")
class _GMfail:
    def __init__(self, *a, **k): pass
    def generate_content(self, *a, **k): raise RuntimeError("429 Your project has exceeded its monthly spending cap")
try:
    bot._disparar_alerta = lambda t, c: disparos.append(t)
    bot._alert_state_ia.update(alerting=False, last_alert_at=None)
    bot.genai.GenerativeModel = _GMok
    ok(quieto(bot._checar_ia) is True and bot._IA_CANARIO["ok"] is True and bot._IA_CANARIO["latencia_ms"] is not None, "C1 sucesso: ok=True com latencia", bot._IA_CANARIO)
    ok(bot._IA_CANARIO["modelo"] == bot.GEMINI_MODEL, "C1 registra o modelo checado")
    bot.genai.GenerativeModel = _GMfail
    ok(quieto(bot._checar_ia) is False and bot._IA_CANARIO["ok"] is False and "spending cap" in (bot._IA_CANARIO["erro"] or ""), "C1 falha: ok=False com o erro (nunca levanta)", bot._IA_CANARIO["erro"])
    quieto(bot._verificar_ia_e_alertar)
    ok(disparos == ["IA do bot Sangali fora do ar"], "C2 1a falha -> alerta", disparos)
    quieto(bot._verificar_ia_e_alertar)
    ok(disparos == ["IA do bot Sangali fora do ar"], "C2 2a falha em seguida -> cooldown (sem 2o alerta)", disparos)
    bot.genai.GenerativeModel = _GMok
    quieto(bot._verificar_ia_e_alertar)
    ok(disparos[-1] == "IA do bot Sangali normalizada" and bot._alert_state_ia["alerting"] is False, "C2 volta -> mensagem de normalizacao", disparos)
    with bot.app.test_client() as c:
        body = c.get("/health").get_json()
    ok(all(k in body for k in ("ia_ok", "ia_latencia_ms", "ia_checada_utc", "ia_erro", "ia_modelo")), "C1 /health expoe os campos do canario", list(body.keys())[:8])
    ok(body.get("ia_ok") is True, "C1 /health reflete o ultimo estado (True apos normalizar)")
finally:
    bot.genai.GenerativeModel = _real_gm; bot._disparar_alerta = _real_disp
    bot._alert_state_ia.update(alerting=False, last_alert_at=None)

# ------------------------------------------------------------------ Q
print("\n### Q — contrato do prompt")
P = open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8").read()
ok("[ALERTA: ... assedio ...]" in P, "Q §15 reconhece a marcacao de abuso")
ok("o sistema remove de `produtos_recomendados` qualquer id fora dessa lista" in P, "Q §4 declara o guard ao modelo")

print("\n" + "=" * 60)
if pulados: print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas: print(f"FALHAS ({len(falhas)}): {falhas}"); sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
