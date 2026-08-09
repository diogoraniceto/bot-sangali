"""Frente 1 — hardening do turno. T0-T4.

T0  prova o bug ANTES do fix (roda no codigo de hoje e no novo): mensagem que
    chega durante um turno em voo era concatenada ao texto JA respondido e
    voltava ao turno seguinte -> os mesmos cards reenviados.
T1  corrida: dois turnos consecutivos veem textos disjuntos.
T2  vazamento: early-return (silencio pos-handoff) nao deixa buffer orfao.
T3  cross-talk: o contexto do turno e por THREAD, nao global de modulo.
T3b guard de tamanho lendo o contexto do turno.
T4  dupla alimentacao: get_history(excluir_id=) nao devolve a msg atual.
T4b cauda do history com dois turnos 'user' adjacentes e colapsada.

    python tests/test_turn_hardening.py

Escrita em banco: T4/T4b inserem algumas linhas em chat_history com user_id
'test_hard_*' e as apagam no finally. Nenhum outro teste toca o banco real.
"""
import os
import sys
import time
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["ENABLE_INPROCESS_SYNC"] = "0"       # nao sobe scheduler/watchdog
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


TEM_REFACTOR = hasattr(bot, "_executar_turno")

# --- Stubs de envio (nenhum WhatsApp sai daqui) ----------------------------
_sent_msgs = []
_sent_media = []
bot.enviar_mensagem_whatsapp = lambda n, t: (_sent_msgs.append((n, t)), {"status": "stub"})[1]
bot.enviar_midia_whatsapp = lambda n, u, c=None: (_sent_media.append((n, u, c)), {"status": "stub"})[1]

_REAL_SUPABASE = bot.supabase
_REAL_SAVE = bot.save_message
_REAL_HANDOFF = bot._em_silencio_pos_handoff
_REAL_EXEC = getattr(bot, "_executar_turno", None)   # salvo ANTES de qualquer stub


# --- Fake supabase (T0/T1/T2 rodam 100% offline) ---------------------------
class _FakeExec:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._d = data

    def select(self, *a, **k): return self
    def insert(self, *a, **k): return self
    def delete(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def like(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def single(self): return self
    def execute(self): return _FakeExec(self._d)


class _FakeSupabase:
    def table(self, name):
        if name == "bot_settings":
            return _FakeQuery({"is_active": True, "system_prompt": "prompt de teste"})
        return _FakeQuery([{"id": "fake-uuid"}])

    def rpc(self, *a, **k):
        return _FakeQuery([])


def _limpar(uid):
    """Tira buffer + timer pendente do uid (senao um Timer de 10s dispara depois)."""
    b = bot.message_buffers.pop(uid, None)
    if b and b.get("timer") is not None:
        try:
            b["timer"].cancel()
        except Exception:
            pass


def _restaurar():
    bot.supabase = _REAL_SUPABASE
    bot.save_message = _REAL_SAVE
    bot._em_silencio_pos_handoff = _REAL_HANDOFF
    if _REAL_EXEC is not None:
        bot._executar_turno = _REAL_EXEC


# =========================================================================
print("### T0 — mensagem que chega durante o turno NAO volta concatenada")
uid = "test_hard_pre"
try:
    bot.supabase = _FakeSupabase()
    bot.save_message = lambda u, r, c: time.sleep(1.5)      # segura o turno em voo
    bot._em_silencio_pos_handoff = lambda u: True           # early-return barato
    bot.message_buffers[uid] = {"text": "oi", "timer": None}
    th = threading.Thread(target=bot.process_and_respond, args=(uid,), daemon=True)
    th.start()
    time.sleep(0.4)                                         # turno em voo
    bot._enqueue_user_message(uid, "tem mais?")
    atual = (bot.message_buffers.get(uid) or {}).get("text")
    ok(atual == "tem mais?",
       "T0 buffer do proximo turno tem SO a msg nova (nao 'oi tem mais?')",
       repr(atual))
    th.join(timeout=10)
finally:
    _limpar(uid)
    _restaurar()


# =========================================================================
print("\n### T1 — corrida: turnos consecutivos veem textos disjuntos")
uid = "test_hard_race"
if not TEM_REFACTOR:
    skip("T1", "_executar_turno ainda nao existe (codigo pre-fix)")
else:
    try:
        vistos = []
        bot._executar_turno = lambda u, txt, fp=None: (vistos.append(txt), time.sleep(1.5))
        bot.message_buffers[uid] = {"text": "oi", "timer": None}
        th = threading.Thread(target=bot.process_and_respond, args=(uid,), daemon=True)
        th.start()
        time.sleep(0.3)
        bot._enqueue_user_message(uid, "tem mais?")
        th.join(timeout=15)
        bot.process_and_respond(uid)                        # drena a msg nova
        ok(vistos == ["oi", "tem mais?"],
           "T1 turnos veem ['oi','tem mais?'] (hoje: ['oi','oi tem mais?'])", vistos)
        ok(uid not in bot.message_buffers, "T1 buffer drenado ao fim dos dois turnos")
    finally:
        _limpar(uid)
        _restaurar()


# =========================================================================
print("\n### T2 — early-return (silencio pos-handoff) nao deixa buffer orfao")
uid = "test_hard_leak"
if not TEM_REFACTOR:
    skip("T2", "_executar_turno ainda nao existe (codigo pre-fix)")
else:
    try:
        bot._executar_turno = _REAL_EXEC                    # corpo real
        bot.supabase = _FakeSupabase()                      # is_active=True garantido
        bot._em_silencio_pos_handoff = lambda u: True
        _sent_msgs.clear(); _sent_media.clear()
        bot.message_buffers[uid] = {"text": "oi", "timer": None}
        bot.process_and_respond(uid)
        ok(uid not in bot.message_buffers, "T2 buffer nao ficou orfao no early-return")
        ok(_sent_msgs == [] and _sent_media == [], "T2 nada foi enviado ao cliente",
           (_sent_msgs, _sent_media))
    finally:
        _limpar(uid)
        _restaurar()


# =========================================================================
print("\n### T3 — contexto do turno e por THREAD (sem cross-talk)")
if not TEM_REFACTOR:
    skip("T3", "_executar_turno ainda nao existe (codigo pre-fix)")
else:
    try:
        capturado = []
        _barreira = threading.Barrier(2, timeout=10)

        def _stub(u, txt, fp=None):
            try:
                _barreira.wait()          # garante os dois turnos SIMULTANEOS
            except Exception:
                pass
            time.sleep(0.2)
            capturado.append((u, bot._ctx_user_id(), bot._ctx_last_msg()))

        bot._executar_turno = _stub
        bot.message_buffers["test_hard_a"] = {"text": "quero baby doll G", "timer": None}
        bot.message_buffers["test_hard_b"] = {"text": "tem no GG?", "timer": None}
        ths = [threading.Thread(target=bot.process_and_respond, args=(u,), daemon=True)
               for u in ("test_hard_a", "test_hard_b")]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=15)
        got = dict((u, (cu, msg)) for (u, cu, msg) in capturado)
        ok(got.get("test_hard_a") == ("test_hard_a", "quero baby doll G"),
           "T3 turno A ve o proprio user_id e o proprio texto", got.get("test_hard_a"))
        ok(got.get("test_hard_b") == ("test_hard_b", "tem no GG?"),
           "T3 turno B ve o proprio user_id e o proprio texto", got.get("test_hard_b"))
        ok(bot._ctx_last_msg() is None,
           "T3 thread principal (fora de turno) nao tem contexto", bot._ctx_last_msg())
    finally:
        _limpar("test_hard_a")
        _limpar("test_hard_b")
        _restaurar()


print("\n### T3b — guard de tamanho le o contexto do turno")
if not hasattr(bot, "_resolver_tamanho_alvo"):
    skip("T3b", "_resolver_tamanho_alvo ainda nao existe (codigo pre-fix)")
else:
    try:
        bot._set_turn_ctx("u", "quero G")
        ok(bot._resolver_tamanho_alvo("GG") == ("G", True),
           "T3b LLM diz GG, cliente disse G -> forca G", bot._resolver_tamanho_alvo("GG"))
        ok(bot._resolver_tamanho_alvo("G") == ("G", False),
           "T3b LLM diz G, cliente disse G -> nao mexe")
        ok(bot._resolver_tamanho_alvo(None) == (None, False),
           "T3b sem tamanho -> (None, False)")
        bot._clear_turn_ctx()
        ok(bot._resolver_tamanho_alvo("GG") == ("GG", False),
           "T3b fora de turno o guard nao inventa correcao", bot._resolver_tamanho_alvo("GG"))
    finally:
        bot._clear_turn_ctx()


# =========================================================================
print("\n### T4 — get_history(excluir_id=) nao devolve a mensagem atual")
uid = "test_hard_hist"
uid_b = "test_hard_hist_b"
_precisa_db = True
try:
    import inspect
    _sig = inspect.signature(bot.get_history)
except Exception:
    _sig = None

if _sig is None or "excluir_id" not in _sig.parameters:
    skip("T4", "get_history ainda nao aceita excluir_id (codigo pre-fix)")
    skip("T4b", "get_history ainda nao aceita excluir_id (codigo pre-fix)")
else:
    try:
        bot.supabase = _REAL_SUPABASE
        bot.save_message = _REAL_SAVE
        bot.supabase.table("chat_history").delete().like("user_id", "test_hard%").execute()

        bot.save_message(uid, "user", "oi"); time.sleep(0.05)
        bot.save_message(uid, "model", "ola!"); time.sleep(0.05)
        mid = bot.save_message(uid, "user", "G")
        ok(mid is not None, "T4 save_message devolve o id da linha inserida", mid)
        h = bot.get_history(uid, excluir_id=mid)
        ok(h == [{"role": "user", "parts": ["oi"]}, {"role": "model", "parts": ["ola!"]}],
           "T4 history sem a mensagem atual (fim da dupla alimentacao)", h)
        h_sem = bot.get_history(uid)
        ok(len(h_sem) == 3 and h_sem[-1]["parts"] == ["G"],
           "T4 sem excluir_id o history ainda traz tudo (contrato preservado)", h_sem)

        # T4b — cauda com dois turnos 'user' adjacentes
        bot.save_message(uid_b, "user", "oi"); time.sleep(0.05)
        bot.save_message(uid_b, "model", "ola!"); time.sleep(0.05)
        bot.save_message(uid_b, "user", "A"); time.sleep(0.05)
        bot.save_message(uid_b, "user", "B"); time.sleep(0.05)
        mid_b = bot.save_message(uid_b, "user", "C")
        hb = bot.get_history(uid_b, excluir_id=mid_b)
        ok([r["role"] for r in hb] == ["user", "model", "user", "user"],
           "T4b pre-condicao: cauda tem dois 'user' adjacentes", hb)
        hb = bot._normalizar_history_para_gemini(hb)
        ok([r["role"] for r in hb] == ["user", "model", "user"] and hb[-1]["parts"] == ["B"],
           "T4b cauda colapsada deixa UM turno user final (o mais recente)", hb)
        ok(hb[0]["role"] == "user", "T4b history comeca em turno user")

        # cabeca: history que comeca em 'model' e podado
        hc = bot._normalizar_history_para_gemini(
            [{"role": "model", "parts": ["x"]}, {"role": "user", "parts": ["y"]}])
        ok(hc == [{"role": "user", "parts": ["y"]}], "T4b cabeca 'model' e podada", hc)
    finally:
        try:
            _REAL_SUPABASE.table("chat_history").delete().like("user_id", "test_hard%").execute()
        except Exception as e:
            print(f"  (cleanup chat_history falhou: {e})")
        _restaurar()


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
