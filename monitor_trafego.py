"""Monitor de campanha: vigia o bot enquanto ha trafego pago rodando.

Existe porque o /health tem um ponto cego conhecido: quando o teto de gasto do
Gemini estoura, TODA cliente recebe "Ops, tive um probleminha tecnico" e o
/health continua respondendo 200 status:ok — ele so olha idade do sync e nulos
de embedding, e o sync nao chama a API quando nada mudou. Em 16/08 isso
aconteceu e nenhum alarme disparou. Com anuncio rodando, cada minuto assim e
clique pago que vira nada.

    python monitor_trafego.py             # foto do momento (ultimas 6h)
    python monitor_trafego.py --horas 24  # janela maior
    python monitor_trafego.py --watch     # fica vigiando, avisa em mudanca

Le o banco de producao e faz UMA chamada de teste ao Gemini. Nao escreve nada.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["ENABLE_INPROCESS_SYNC"] = "0"
os.environ.setdefault("WHATSAPP_PROVIDER", "uazapi")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

import bot  # noqa: E402

FALLBACK = "probleminha tecnico"
LATENCIA_RUIM_MS = 20000          # lead que veio de anuncio nao espera 20s


def _agora():
    return datetime.now(timezone.utc)


def canario_ia():
    """O teste que o /health NAO faz: a IA responde?"""
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        m = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
        t0 = time.time()
        r = m.generate_content("responda so: ok")
        return True, f"{(r.text or '').strip()[:12]} ({time.time()-t0:.1f}s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def coletar(horas):
    desde = (_agora() - timedelta(hours=horas)).isoformat()
    ch = (bot.supabase.table("chat_history")
          .select("created_at,user_id,role,content")
          .gte("created_at", desde).order("created_at").limit(2000).execute()).data or []
    turns = (bot.supabase.table("bot_turns")
             .select("created_at,user_id,latency_ms,fallback_used,error,output_format,tool_calls")
             .gte("created_at", desde).order("created_at").limit(2000).execute()).data or []
    try:
        hand = (bot.supabase.table("conversation_handoffs")
                .select("created_at,user_id,motivo")
                .gte("created_at", desde).limit(200).execute()).data or []
    except Exception:
        hand = []
    return ch, turns, hand


def relatorio(horas):
    ch, turns, hand = coletar(horas)
    contatos = {}
    for x in ch:
        contatos.setdefault(x["user_id"], []).append(x)

    novos = [u for u, msgs in contatos.items()
             if any(m["role"] == "user" for m in msgs)]
    erros = [x for x in ch if FALLBACK in (x.get("content") or "")]
    lentos = [t for t in turns if (t.get("latency_ms") or 0) > LATENCIA_RUIM_MS]
    com_erro = [t for t in turns if t.get("error")]
    sem_busca = [t for t in turns
                 if not (t.get("tool_calls") or [])]

    ok_ia, detalhe_ia = canario_ia()

    print("=" * 66)
    print(f"MONITOR DE CAMPANHA — ultimas {horas}h — {_agora():%d/%m %H:%M} UTC")
    print("=" * 66)
    print(f"  IA (canario)      : {'OK' if ok_ia else '*** FORA DO AR ***'}  {detalhe_ia}")
    print(f"  contatos          : {len(novos)}")
    print(f"  turnos            : {len(turns)}")
    print(f"  handoffs          : {len(hand)}")
    print(f"  respostas de ERRO : {len(erros)}   <-- cliente viu 'probleminha tecnico'")
    print(f"  turnos com erro   : {len(com_erro)}")
    print(f"  turnos > {LATENCIA_RUIM_MS//1000}s      : {len(lentos)} de {len(turns)}")

    if not ok_ia:
        print()
        print("  !!! A IA NAO RESPONDE. Todo lead que chegar agora recebe mensagem")
        print("  !!! de erro. O /health NAO acusa isso. Veja https://ai.studio/spend")

    if erros:
        print()
        print("  RESPOSTAS DE ERRO (cada uma e um clique pago perdido):")
        for e in erros[:10]:
            print(f"    {e['created_at'][:19]}  {e['user_id']}")

    if contatos:
        print()
        print("  CONVERSAS:")
        for u, msgs in sorted(contatos.items(), key=lambda kv: kv[1][0]["created_at"]):
            do_cliente = [m for m in msgs if m["role"] == "user"]
            falhou = any(FALLBACK in (m.get("content") or "") for m in msgs)
            pediu_humano = any(h.get("user_id") == u for h in hand)
            marca = "  [ERRO]" if falhou else ("  [handoff]" if pediu_humano else "")
            primeira = (do_cliente[0].get("content") or "")[:44] if do_cliente else ""
            print(f"    {msgs[0]['created_at'][11:19]}  {u:<14} "
                  f"{len(do_cliente):>2} msgs  {primeira!r}{marca}")
    else:
        print()
        print("  Nenhuma conversa na janela. Com anuncio no ar, ou o lead ainda nao")
        print("  chegou, ou o anuncio nao esta entregando — confira no Gerenciador.")
    print()
    return len(turns), len(erros), ok_ia


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=float, default=6)
    ap.add_argument("--watch", action="store_true", help="vigia continuamente")
    ap.add_argument("--intervalo", type=int, default=60)
    a = ap.parse_args()

    if not a.watch:
        relatorio(a.horas)
        return

    anterior = None
    while True:
        estado = relatorio(a.horas)
        if anterior and estado != anterior:
            print(">>> MUDOU desde a ultima checagem <<<")
        anterior = estado
        time.sleep(a.intervalo)


if __name__ == "__main__":
    main()
