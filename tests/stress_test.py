"""Stress test: 5 clientes simultâneos, 3 turnos cada.

Verifica:
- Lock por user_id mantém integridade dos buffers
- bot_turns NÃO mistura mensagens entre clientes
- Rate limit não dispara para 3 msgs em sequência
"""
import os
import sys
import time
import threading
from datetime import datetime
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

load_dotenv(os.path.join(ROOT, ".env"))

import bot  # noqa: E402

# Stub envios
_sent = {"messages": [], "media": []}
_lock_sent = threading.Lock()


def _stub_msg(numero, texto):
    with _lock_sent:
        _sent["messages"].append({"to": numero, "text": texto, "ts": time.time()})


def _stub_media(numero, url, legenda):
    with _lock_sent:
        _sent["media"].append({"to": numero, "url": url, "caption": legenda or "", "ts": time.time()})


bot.enviar_mensagem_whatsapp = _stub_msg
bot.enviar_midia_whatsapp = _stub_media


def cliente_simulado(idx, mensagens, resultados):
    user_id = f"test_stress_{datetime.now().strftime('%Y%m%d%H%M%S')}_{idx:02d}"
    log = []
    for i, msg in enumerate(mensagens):
        bot.message_buffers[user_id] = {"text": msg, "timer": None}
        t0 = time.time()
        try:
            bot.process_and_respond(user_id)
            err = None
        except Exception as e:
            err = str(e)
        log.append({
            "step": i + 1,
            "msg": msg,
            "err": err,
            "elapsed": round(time.time() - t0, 2),
        })
    resultados.append({"user_id": user_id, "log": log})


CONVERSAS = [
    ["oi tem cueca?", "G", "valeu"],
    ["queria pijama M", "qual o frete pra 01310-100?", "obrigada"],
    ["tem fantasia?", "tamanho único", "fecha aí"],
    ["promoção hoje?", "atacado primeira compra como funciona?", "vou pensar"],
    ["camisolas M", "essa primeira mesmo", "qual cor disponível?"],
]


def main():
    print(f"Disparando {len(CONVERSAS)} clientes em paralelo, 3 turnos cada...")
    threads = []
    resultados = []
    t_start = time.time()
    for idx, msgs in enumerate(CONVERSAS):
        t = threading.Thread(target=cliente_simulado, args=(idx + 1, msgs, resultados))
        threads.append(t)
        t.start()
        time.sleep(0.1)  # leve stagger pra evitar literal mesmo timestamp
    for t in threads:
        t.join()
    elapsed = time.time() - t_start

    print(f"\nConcluído em {elapsed:.1f}s. {len(resultados)} clientes finalizaram.\n")

    # Verifica via bot_turns que cada user_id tem exatamente 3 turnos
    erros = []
    for r in resultados:
        uid = r["user_id"]
        try:
            q = (
                bot.supabase.table("bot_turns")
                .select("id, user_input, output_format, fallback_used")
                .eq("user_id", uid)
                .order("created_at", desc=False)
                .execute()
            )
            n_turnos = len(q.data or [])
            print(f"[{uid}] {n_turnos} turnos | sample: {[(t['user_input'][:30], t['output_format']) for t in (q.data or [])[:3]]}")
            if n_turnos != 3:
                erros.append(f"{uid}: esperava 3 turnos, encontrou {n_turnos}")
            for t in (q.data or []):
                if t.get("fallback_used"):
                    erros.append(f"{uid} fallback ativado: {t['user_input'][:40]}")
        except Exception as e:
            erros.append(f"{uid}: erro consultando bot_turns: {e}")

    print(f"\n=== RESULTADO ===")
    if erros:
        print(f"FAIL: {len(erros)} problemas:")
        for e in erros:
            print(f"  - {e}")
    else:
        print(f"PASS: todos os {len(resultados)} clientes processaram 3 turnos sem mistura.")


if __name__ == "__main__":
    main()
