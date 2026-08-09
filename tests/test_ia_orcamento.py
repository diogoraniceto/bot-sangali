"""Teto de tempo TOTAL do turno: nenhuma chamada ao Gemini pode prender o turn_lock.

Zero rede: Gemini e Supabase sao stubbados. Incidente que motivou o teste: uma
chamada ficou ~11 h pendurada na 3a tentativa do retry, segurando o turn_lock do
cliente (todas as mensagens seguintes dele ficaram na fila para sempre).

    python tests/test_ia_orcamento.py
"""
import os, sys, time, threading
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["ENABLE_INPROCESS_SYNC"] = "0"
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


# ---------- stubs ----------
class FakeChat:
    """Simula chat.send_message: pendura, levanta, ou responde."""
    def __init__(self, comportamento, texto="{}"):
        self.comportamento = comportamento
        self.texto = texto
        self.history = []
        self.ctx_visto = None
        self.chamadas = 0

    def send_message(self, msg, request_options=None):
        self.chamadas += 1
        self.timeout_pedido = (request_options or {}).get("timeout")
        # o que a TOOL enxergaria: as tools rodam dentro desta chamada
        self.ctx_visto = (bot._ctx_user_id(), bot._ctx_msg_cliente())
        c = self.comportamento
        if callable(c):
            c = c(self.chamadas)
        if c == "pendura":
            time.sleep(3600)
        if isinstance(c, Exception):
            raise c
        return type("Resp", (), {"text": self.texto, "usage_metadata": None})()


class FakeModel:
    def __init__(self, chat): self._chat = chat
    def start_chat(self, **kw): return self._chat


class _Q:
    def __init__(self, data): self._data = data
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def single(self, *a, **k): return self
    def insert(self, *a, **k): return _Q([{"id": "row-1"}])
    def delete(self, *a, **k): return self
    def execute(self): return type("R", (), {"data": self._data})()


class FakeSupabase:
    def table(self, nome):
        if nome == "bot_settings":
            return _Q({"is_active": True, "system_prompt": "voce e a Luna"})
        return _Q([])


def instalar(chat, orcamento):
    bot.supabase = FakeSupabase()
    bot.genai.GenerativeModel = lambda *a, **k: FakeModel(chat)
    bot.TURNO_ORCAMENTO_S = orcamento
    # em producao o piso e 5s (nao vale abrir tentativa com menos que isso); aqui
    # baixamos so para o teste nao levar 2 min
    bot._IA_TENTATIVA_MIN_S = 0.5
    enviadas.clear()
    logs.clear()


enviadas, logs = [], []
_originais = (bot.supabase, bot.genai.GenerativeModel, bot.TURNO_ORCAMENTO_S,
              bot.enviar_mensagem_whatsapp, bot.log_turn, bot._em_silencio_pos_handoff,
              bot.get_history, bot.save_message, bot._IA_TENTATIVA_MIN_S)
bot.enviar_mensagem_whatsapp = lambda uid, txt, **k: enviadas.append((uid, txt))
bot.log_turn = lambda **kw: logs.append(kw)
bot._em_silencio_pos_handoff = lambda uid: False
bot.get_history = lambda uid, **kw: []
bot.save_message = lambda uid, role, content: "row-1"

try:
    print("### G. _ia_send_com_teto (unidade)")
    # G1 — chamada pendurada nao passa do teto
    chat = FakeChat("pendura")
    bot._set_turn_ctx("u_g1", "quero camisola G")
    t0 = time.perf_counter()
    try:
        bot._ia_send_com_teto(chat, "oi", 1.5)
        estourou = False
    except bot.TurnoOrcamentoEsgotado:
        estourou = True
    dt = time.perf_counter() - t0
    ok(estourou, "G1 chamada pendurada levanta TurnoOrcamentoEsgotado")
    ok(1.4 < dt < 3.0, f"G1 devolveu em {dt:.2f}s (teto 1,5s), nao esperou o SDK", dt)

    # G2 — as tools rodam na thread de trabalho e PRECISAM enxergar o contexto
    chat = FakeChat("ok")
    bot._set_turn_ctx("u_g2", "tem no GG?")
    bot._ia_send_com_teto(chat, "oi", 10)
    ok(chat.ctx_visto == ("u_g2", "tem no GG?"),
       "G2 contexto do turno visivel dentro da thread de trabalho", chat.ctx_visto)
    ok(bot._ctx_user_id() == "u_g2",
       "G2 a thread chamadora mantem o proprio contexto", bot._ctx_user_id())

    # G3 — o timeout pedido ao SDK nunca excede o que sobrou do orcamento
    chat = FakeChat("ok")
    bot._ia_send_com_teto(chat, "oi", 7)
    ok(chat.timeout_pedido == 7, "G3 teto menor que IA_TIMEOUT_S vira o timeout do SDK",
       chat.timeout_pedido)
    chat = FakeChat("ok")
    bot._ia_send_com_teto(chat, "oi", 999)
    ok(chat.timeout_pedido == bot.IA_TIMEOUT_S,
       "G3b teto folgado mantem IA_TIMEOUT_S", chat.timeout_pedido)

    # G4 — excecao do SDK atravessa (o retry existente continua funcionando)
    chat = FakeChat(RuntimeError("503 Service Unavailable"))
    try:
        bot._ia_send_com_teto(chat, "oi", 10)
        propagou = False
    except RuntimeError as e:
        propagou = "503" in str(e)
    ok(propagou, "G4 erro do SDK e re-levantado na thread chamadora")
    bot._clear_turn_ctx()

    print("\n### H. _executar_turno respeita o orcamento (Gemini stubbado)")
    # H1 — turno pendurado termina no teto e cai no fallback gracioso
    chat = FakeChat("pendura")
    instalar(chat, orcamento=2.0)
    t0 = time.perf_counter()
    bot._set_turn_ctx("u_h1", "oi")
    bot._executar_turno("u_h1", "oi")
    bot._clear_turn_ctx()
    dt = time.perf_counter() - t0
    ok(dt < 6.0, f"H1 turno pendurado terminou em {dt:.2f}s (orcamento 2s)", dt)
    ok(enviadas and "probleminha tecnico" in enviadas[-1][1],
       "H1 cliente recebeu o fallback gracioso", enviadas)
    ok(logs and logs[-1].get("output_format") == "error"
       and "orcamento" in (logs[-1].get("error") or "").lower(),
       "H1 log_turn registra o estouro do orcamento", logs[-1] if logs else None)
    ok(chat.chamadas == 1, "H1 nao houve retry do estouro (teto e global)", chat.chamadas)

    # H2 — retry transitorio continua funcionando dentro do orcamento
    chat = FakeChat(lambda n: RuntimeError("504 Deadline Exceeded") if n == 1 else "ok",
                    texto='{"resposta_cliente":"oi!","produtos_recomendados":[]}')
    instalar(chat, orcamento=60.0)
    bot._set_turn_ctx("u_h2", "oi")
    bot._executar_turno("u_h2", "oi")
    bot._clear_turn_ctx()
    ok(chat.chamadas == 2, "H2 erro transitorio ainda gera 1 retry", chat.chamadas)
    ok(not any("probleminha" in t for _, t in enviadas),
       "H2 sem fallback: a 2a tentativa respondeu", enviadas)

    # H3 — orcamento curto ABORTA as tentativas restantes (nao dorme 2s+4s a toa)
    chat = FakeChat(RuntimeError("504 Deadline Exceeded"))
    instalar(chat, orcamento=3.0)
    t0 = time.perf_counter()
    bot._set_turn_ctx("u_h3", "oi")
    bot._executar_turno("u_h3", "oi")
    bot._clear_turn_ctx()
    dt = time.perf_counter() - t0
    ok(chat.chamadas < 3, f"H3 3a tentativa abortada pelo orcamento ({chat.chamadas} chamadas)",
       chat.chamadas)
    ok(dt < 6.0, f"H3 turno inteiro em {dt:.2f}s", dt)
    ok(enviadas and "probleminha tecnico" in enviadas[-1][1],
       "H3 cliente recebeu o fallback gracioso", enviadas)

    print("\n### I. o turn_lock e liberado (era o dano real do pendurado)")
    chat = FakeChat("pendura")
    instalar(chat, orcamento=2.0)
    UID = "u_i1"
    bot.message_buffers[UID] = {"text": "oi", "timer": None}
    bot.process_and_respond(UID)
    lock = bot._get_turn_lock(UID)
    livre = lock.acquire(blocking=False)
    if livre:
        lock.release()
    ok(livre, "I1 turn_lock livre depois do turno pendurado")
    ok(UID not in bot.message_buffers, "I1b buffer drenado", list(bot.message_buffers))
    # o proximo turno do mesmo cliente nao espera
    chat2 = FakeChat("ok", texto='{"resposta_cliente":"oi!","produtos_recomendados":[]}')
    instalar(chat2, orcamento=30.0)
    bot.message_buffers[UID] = {"text": "e ai?", "timer": None}
    t0 = time.perf_counter()
    bot.process_and_respond(UID)
    dt = time.perf_counter() - t0
    ok(dt < 3.0 and chat2.chamadas == 1,
       f"I2 turno seguinte do mesmo cliente roda na hora ({dt:.2f}s)", (dt, chat2.chamadas))
finally:
    (bot.supabase, bot.genai.GenerativeModel, bot.TURNO_ORCAMENTO_S,
     bot.enviar_mensagem_whatsapp, bot.log_turn, bot._em_silencio_pos_handoff,
     bot.get_history, bot.save_message, bot._IA_TENTATIVA_MIN_S) = _originais
    bot._clear_turn_ctx()

vivas = [t.name for t in threading.enumerate() if t.name == "ia-send"]
print(f"\n(threads 'ia-send' abandonadas ainda vivas: {len(vivas)} — sao daemon, "
      f"morrem com o processo)")

print("\n" + ("=" * 60))
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
