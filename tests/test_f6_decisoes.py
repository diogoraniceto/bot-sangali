"""Frente 6 — as 4 decisoes do dono da loja, fixadas em teste.

D1  tamanho COMPOSTO ('P/M','G/GG') -> a Luna avisa que serve nos dois (prompt).
D2  motivo `pedido_foto`: a atendente le "QUER MAIS FOTOS" em portugues, nao o
    slug; o cliente recebe a frase aprovada pelo dono.
D3  silencio pos-handoff = DESCARTAR: a msg do cliente e PERSISTIDA (a atendente
    le), nada e enviado, o buffer nao vaza, e nada responde a ela depois.
D4  msg nova durante resposta em voo = responder as duas, em ORDEM.
    (a corrida em si esta em test_turn_hardening.py T0/T1; aqui fica o que
    aquele arquivo nao cobre: a persistencia no silencio e o contrato do prompt.)

P6  contrato do prompt: as edicoes de F6 estao TODAS aplicadas e o arquivo local
    e igual ao que esta no banco (a defasagem entre os dois ja custou uma rodada).

    python tests/test_f6_decisoes.py

Nao escreve nada no banco. So LE bot_settings (e pula se nao houver credencial).
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


PROMPT_PATH = os.path.join(ROOT, "prompt_luna_v2.txt")
with open(PROMPT_PATH, encoding="utf-8") as f:
    PROMPT = f.read()

# A frase do caso 5 e decisao do dono da loja (2026-08-09). Se alguem mudar o
# texto, este teste falha de proposito: a redacao foi escolhida porque NAO
# promete foto nova — prometer reprovou `photo_transparency` na eval.
FRASE_PEDIDO_FOTO = "Já te chamo alguém do time pra te mostrar melhor essa peça"


# =========================================================================
print("### D1 — tamanho composto: a Luna avisa que serve nos dois")
ok("filtro_aplicado" in PROMPT,
   "D1 prompt ensina que `filtro_aplicado` e a verdade do que foi consultado")
ok('"P/M"' in PROMPT and '"G/GG"' in PROMPT,
   "D1 prompt cita os tamanhos compostos reais do catalogo")
ok("serve nos dois" in PROMPT,
   "D1 prompt manda dizer que a peca composta serve nos dois tamanhos")
ok("ÚNICO" in PROMPT, "D1 prompt cobre tambem o tamanho ÚNICO")
# A proibicao tem de ser ESCOPADA a claims de disponibilidade. A versao absoluta
# ("nunca afirme NADA sobre um tamanho fora de filtro_aplicado") contradizia a
# decisao (b) do dono, porque 'P' nao esta em filtro_aplicado numa busca por M —
# o modelo resolveria o conflito a favor do NUNCA e calaria o tamanho da peca.
ok(re.search(r"NUNCA diga que um tamanho TEM ou N[ÃA]O TEM estoque", PROMPT) is not None,
   "D1 a proibicao e escopada a TEM/NAO TEM estoque (nao um NUNCA absoluto)")
ok("PERMITIDO e esperado" in PROMPT,
   "D1 a excecao do tamanho composto e explicita no MESMO paragrafo da proibicao")
ok(re.search(r"NUNCA afirme nada sobre um tamanho", PROMPT) is None,
   "D1 a versao absoluta da proibicao (que contradizia a decisao (b)) nao voltou")

# O guard do F2 continua sendo a defesa em profundidade: composto NAO vira
# tamanho pedido. `_tokens_tamanho` normaliza 'P/M' nos dois tokens.
if hasattr(bot, "_tokens_tamanho"):
    ok(set(bot._tokens_tamanho("P/M")) == {"P", "M"},
       "D1 'P/M' normaliza em {P,M} (por isso a peca aparece na busca por M)",
       bot._tokens_tamanho("P/M"))
    ok(set(bot._tokens_tamanho("G/GG")) == {"G", "GG"},
       "D1 'G/GG' normaliza em {G,GG}", bot._tokens_tamanho("G/GG"))
else:
    skip("D1 tokens", "_tokens_tamanho nao existe (codigo pre-F2)")


# =========================================================================
print("\n### D2 — pedido_foto: atendente le portugues, cliente recebe a frase do dono")
if not hasattr(bot, "_MOTIVO_LABEL"):
    skip("D2", "_MOTIVO_LABEL nao existe (codigo pre-F6)")
else:
    _REAL_HIST = bot.get_history
    try:
        bot.get_history = lambda u, limit=8, excluir_id=None: []   # sem banco
        msg = bot._montar_mensagem_operador(
            "5527999", "pedido_foto",
            "Cliente quer mais fotos da Camisola Helena (cod. 8147660).",
            "Camisola Helena")
        ok("MAIS FOTOS" in msg.upper(),
           "D2 a atendente le que o cliente QUER MAIS FOTOS", msg[:120])
        ok("pedido_foto" not in msg,
           "D2 o slug tecnico NAO aparece na mensagem da atendente", msg[:120])
        ok("Camisola Helena" in msg,
           "D2 o resumo com a peca chega junto (a atendente sabe QUAL peca)")

        # motivo desconhecido degrada para o slug — esquecer de mapear nao quebra
        msg2 = bot._montar_mensagem_operador("5527999", "motivo_que_nao_existe", "r", "")
        ok("motivo_que_nao_existe" in msg2,
           "D2 motivo nao mapeado cai no proprio slug (degrada, nao quebra)", msg2[:120])

        # os 4 motivos antigos seguem mapeados (nao regredimos os casos 1-4)
        for m in ("pedido_humano", "fechamento_venda", "confusao_repetida", "medo_fraude"):
            ok(m in bot._MOTIVO_LABEL, f"D2 motivo '{m}' segue mapeado")
    finally:
        bot.get_history = _REAL_HIST

ok(FRASE_PEDIDO_FOTO in PROMPT,
   "D2 prompt traz a frase EXATA aprovada pelo dono para o cliente")
ok("pedido_foto" in PROMPT, "D2 prompt define o motivo `pedido_foto`")
ok(re.search(r"TIRAR ou MANDAR uma foto NOVA", PROMPT, re.IGNORECASE) is not None,
   "D2 prompt proibe prometer foto NOVA (a frase do dono nao promete)")
_frase_ctx = PROMPT[max(0, PROMPT.find(FRASE_PEDIDO_FOTO) - 400):
                    PROMPT.find(FRASE_PEDIDO_FOTO) + 200]
ok("tirar" not in _frase_ctx.lower(),
   "D2 a frase do caso 5 nao esta cercada de promessa de tirar foto")


# =========================================================================
print("\n### D3 — silencio pos-handoff DESCARTA (mas persiste para a atendente)")
if not hasattr(bot, "_executar_turno"):
    skip("D3", "_executar_turno nao existe (codigo pre-F1)")
else:
    _REAL = dict(supabase=bot.supabase, save=bot.save_message,
                 sil=bot._em_silencio_pos_handoff, env=bot.enviar_mensagem_whatsapp)
    uid = "test_f6_silencio"
    try:
        salvas = []
        enviadas = []

        class _Q:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def single(self): return self
            def execute(self): return type("R", (), {"data": {"is_active": True,
                                                              "system_prompt": "p"}})()

        class _SB:
            def table(self, n): return _Q()

        bot.supabase = _SB()
        bot.save_message = lambda u, r, c: (salvas.append((u, r, c)), "fake-id")[1]
        bot._em_silencio_pos_handoff = lambda u: True
        bot.enviar_mensagem_whatsapp = lambda n, t: enviadas.append((n, t))

        bot.message_buffers[uid] = {"text": "manda mais fotos", "timer": None}
        bot.process_and_respond(uid)

        ok([s[2] for s in salvas] == ["manda mais fotos"],
           "D3 a msg do cliente E PERSISTIDA em chat_history (a atendente le)", salvas)
        ok(enviadas == [], "D3 a Luna NAO responde durante o silencio", enviadas)
        ok(uid not in bot.message_buffers,
           "D3 buffer drenado: a msg nao fica acumulando (era o bug dos 8,9 dias)")

        # e o turno SEGUINTE nao reenvia o texto antigo (descarte, nao fila)
        salvas.clear()
        bot.process_and_respond(uid)
        ok(salvas == [],
           "D3 turno seguinte sem buffer nao ressuscita a msg descartada", salvas)
    finally:
        b = bot.message_buffers.pop(uid, None)
        if b and b.get("timer") is not None:
            try:
                b["timer"].cancel()
            except Exception:
                pass
        bot.supabase = _REAL["supabase"]
        bot.save_message = _REAL["save"]
        bot._em_silencio_pos_handoff = _REAL["sil"]
        bot.enviar_mensagem_whatsapp = _REAL["env"]

ok(bot.HANDOFF_SILENCIO_MIN == 120,
   "D3 janela de silencio segue 120 min (timer fixo, sem sinal da atendente)",
   bot.HANDOFF_SILENCIO_MIN)


# --- D3b: os avisos AVULSOS tambem calam (achado da revisao adversarial) -----
# A checagem de silencio vivia SO dentro de _executar_turno. Tres caminhos falavam
# por fora dela: audio ilegivel, foto que nao abre, e o except de process_and_respond.
# Cliente com a atendente humana mandava um audio ruim e o bot "em silencio" respondia.
print("\n### D3b — aviso avulso (audio/foto/erro) respeita o silencio")
if not hasattr(bot, "_avisar_cliente"):
    skip("D3b", "_avisar_cliente nao existe (codigo pre-fix)")
else:
    _REAL_ENV = bot.enviar_mensagem_whatsapp
    _REAL_SIL = bot._em_silencio_pos_handoff
    try:
        saiu = []
        bot.enviar_mensagem_whatsapp = lambda n, t: (saiu.append((n, t)), "wamid")[1]

        bot._em_silencio_pos_handoff = lambda u: True
        bot._avisar_cliente("5527999", "Não consegui ouvir seu áudio 😅")
        ok(saiu == [], "D3b em silencio o aviso NAO sai", saiu)

        bot._em_silencio_pos_handoff = lambda u: False
        bot._avisar_cliente("5527999", "Não consegui ouvir seu áudio 😅")
        ok(len(saiu) == 1, "D3b fora do silencio o aviso sai normalmente", saiu)

        # falha ao consultar o handoff nao pode emudecer o bot para todo mundo
        def _boom(u):
            raise RuntimeError("banco fora")
        bot._em_silencio_pos_handoff = _boom
        bot._avisar_cliente("5527999", "teste")
        ok(len(saiu) == 2,
           "D3b se a consulta de handoff falhar, degrada para ENVIAR (nao emudece)", saiu)
    finally:
        bot.enviar_mensagem_whatsapp = _REAL_ENV
        bot._em_silencio_pos_handoff = _REAL_SIL

# e os 3 caminhos usam mesmo o helper (senao o fix nao vale de nada)
import inspect as _insp  # noqa: E402
_src_midia = _insp.getsource(bot._processar_midia_async)
ok(_src_midia.count("_avisar_cliente(") == 2 and "enviar_mensagem_whatsapp(" not in _src_midia,
   "D3b os 2 fallbacks de midia (audio/foto) passam por _avisar_cliente",
   _src_midia.count("_avisar_cliente("))

# A checagem tem de vir ANTES do download/transcricao: a decisao 3 manda persistir
# a mensagem para a atendente, e transcrever para jogar fora queima quota multimodal.
_i_sil = _src_midia.find("_em_silencio_pos_handoff")
_i_baixar = _src_midia.find("_cloud_baixar_midia")
ok(_i_sil != -1 and _i_baixar != -1 and _i_sil < _i_baixar,
   "D3b o silencio e checado ANTES de baixar/transcrever (nao queima Gemini no handoff)",
   (_i_sil, _i_baixar))
ok("save_message(" in _src_midia,
   "D3b a midia recebida no handoff e PERSISTIDA (a atendente ve que veio audio/foto)")
_src_par = _insp.getsource(bot.process_and_respond)
ok("_avisar_cliente(" in _src_par,
   "D3b o except de process_and_respond passa por _avisar_cliente")
# a mensagem do OPERADOR nao pode ser silenciada — ela existe para o handoff
_src_tr = _insp.getsource(bot.criar_tool_transferir)
ok("_avisar_cliente(" not in _src_tr,
   "D3b o aviso ao OPERADOR nao foi silenciado por engano (ele TEM de sair no handoff)")


# =========================================================================
print("\n### D4 — responder as duas, em ordem (lock antes do dreno)")
_src = ""
try:
    import inspect
    _src = inspect.getsource(bot.process_and_respond)
except Exception:
    pass
if not _src:
    skip("D4", "nao consegui ler o fonte de process_and_respond")
else:
    i_lock = _src.find("with turn_lock:")
    i_pop = _src.find("message_buffers.pop")
    ok(i_lock != -1 and i_pop != -1 and i_lock < i_pop,
       "D4 adquire o turn_lock ANTES de drenar o buffer (msg nova entra no mesmo turno)",
       (i_lock, i_pop))
ok("tem mais" in PROMPT.lower() and "DIFERENTES" in PROMPT,
   "D4/P6.4 prompt manda mostrar produtos DIFERENTES em 'tem mais?'")


# =========================================================================
print("\n### P6 — contrato das edicoes do prompt")
ok("em 5 casos" in PROMPT, "P6.3 §15 declara 5 casos de transferencia")
ok("um dos 5 acima" in PROMPT, "P6.3 §15 argumentos: 'um dos 5 acima'")
ok("fora desses 5" in PROMPT, "P6.3 §15 regra: 'fora desses 5'")
for sobra in ("em 4 casos", "um dos 4 acima", "fora desses 4"):
    ok(sobra not in PROMPT, f"P6.3 sobra do texto antigo removida: '{sobra}'")

ok("mostrar_fotos_produto" in PROMPT, "P6.3 prompt conhece a tool de fotos")
ok("n_fotos" in PROMPT, "P6.3 prompt conhece `n_fotos`")
ok("vai_enviar" in PROMPT and "restantes" in PROMPT and "sem_novas" in PROMPT,
   "P6.3 prompt conhece o contrato de retorno da tool de fotos")
ok("sem_foto" in PROMPT, "P6.3 prompt trata o status `sem_foto` sem oferecer atendente")

ok('boost de "mais vendidos"' not in PROMPT,
   "P6.1 afirmacao FALSA sobre boost de bestsellers foi removida")
ok("destaque: true" in PROMPT or "`destaque: true`" in PROMPT,
   "P6.1 prompt conhece o selo `destaque: true` da F5")
ok("8 produtos DISTINTOS" in PROMPT,
   "P6.1 prompt sabe que a busca devolve ate 8 distintos (era 'top-10 com boost')")
ok("SUBSTANTIVO" in PROMPT,
   "P6.1 regra anti-'fantasia': adjetivo sensual nao define categoria")
ok("FOTOS_SOB_DEMANDA" not in PROMPT,
   "P6 nome de env var nao vaza para o prompt do modelo")

# O prompt do modelo NAO deve citar URL de imagem como algo que ele escreve
ok(re.search(r"NUNCA inclua URLs", PROMPT) is not None,
   "P6 §0 mantem a proibicao de URL em resposta_texto")


# =========================================================================
print("\n### P6.5 — arquivo local == prompt no banco (fim da defasagem)")
try:
    r = bot.supabase.table("bot_settings").select("system_prompt").eq("id", 1).single().execute()
    db_prompt = (r.data or {}).get("system_prompt") or ""
except Exception as e:
    db_prompt = None
    skip("P6.5", f"banco inacessivel ({type(e).__name__})")

if db_prompt is not None:
    if db_prompt == PROMPT:
        ok(True, "P6.5 prompt_luna_v2.txt e byte-identico ao de bot_settings")
    else:
        ok(False,
           "P6.5 prompt_luna_v2.txt e o do banco DIVERGEM — rode inserir_prompt.py",
           f"local={len(PROMPT)} chars, banco={len(db_prompt)} chars")


# =========================================================================
# Instrucao DINAMICA de curadoria (padrao §3.9): a regra generica do §4 nao
# impedia o modelo de completar a lista com vizinhos. Estes testes batem no banco
# real porque o que importa e o gatilho com o catalogo de verdade.
print("\n### C — instrucao dinamica de curadoria (fantasia tem 1 unidade na Matriz)")


def _instr_curadoria(termo, tam):
    r = bot.consultar_estoque_supabase(termo, tamanho=tam, id_loja="244033")
    if r.get("status") != "sucesso":
        return None, r.get("status")
    return (r.get("filtro_aplicado") or {}).get("instrucao_curadoria"), len(r.get("produtos") or [])


try:
    ins, n = _instr_curadoria("fantasia", None)
    if n in (None, "erro"):
        skip("C1", f"busca indisponivel ({n})")
    else:
        ok(ins is not None and "apenas 1" in ins and "40214981" in ins,
           "C1 'fantasia' (1 item na lista) -> instrucao NOMEIA o id do unico item",
           (ins or "")[:110])

    # MUDOU EM 16/08 (tamanho UNICO passou a entrar em busca com tamanho — opcao A).
    # ANTES: a unica fantasia da Matriz e tamanho UNICO, era filtrada fora, sobrava
    # ZERO fantasia, e a instrucao mandava `produtos_recomendados = []`. Ou seja:
    # quem pedia "fantasia M" nao recebia NADA, tendo a peca em estoque.
    # AGORA: ela sobrevive ao filtro e a instrucao cai no ramo "recomende SOMENTE
    # esse id". O invariante que importa NAO mudou e continua sendo o assert — a
    # Luna nao pode completar a lista com vizinhos de outra categoria.
    ins, n = _instr_curadoria("fantasia de enfermeira", "M")
    ok(ins is not None and "40214981" in ins and "SOMENTE" in ins,
       "C2 'fantasia M' -> a fantasia UNICO sobrevive e a instrucao NOMEIA o id",
       (ins or "")[:110])
    ok(ins is not None and "OUTRA categoria" in ins and "NAO complete a lista" in ins,
       "C2 ... e continua proibindo completar com vizinhos (o invariante nao mudou)",
       (ins or "")[:110])

    # O par de instrucoes tem de chegar junto: sem a de tamanho, o modelo ve uma
    # peca 'UNICO' respondendo a um pedido 'M' e a descarta ou mente o tamanho.
    r_c2 = bot.consultar_estoque_supabase("fantasia de enfermeira", tamanho="M",
                                          id_loja="244033")
    fa_c2 = (r_c2.get("filtro_aplicado") or {})
    ok("regulagem" in (fa_c2.get("instrucao_tamanho_unico") or "").lower(),
       "C2 ... acompanhada da instrucao de regulagem (senao o modelo curaria fora)",
       (fa_c2.get("instrucao_tamanho_unico") or "")[:90])

    # Categoria bem estocada: o alerta seria ruido, e pior — faria a Luna deixar
    # venda na mesa. Silencio aqui e requisito, nao acidente.
    for termo, tam in (("calcinha sem costura", "M"), ("camisola", "GG")):
        ins, n = _instr_curadoria(termo, tam)
        ok(ins is None,
           f"C3 '{termo}' ({tam}) e categoria estocada -> SEM instrucao (nao suprime venda)",
           (ins or "")[:90])
except Exception as e:
    skip("C1-C3", f"banco/Gemini indisponivel ({type(e).__name__}: {str(e)[:60]})")

ok("instrucao_curadoria" in PROMPT,
   "C4 prompt sabe que `instrucao_curadoria` e obrigatoria quando vier")
ok("instrucao_tamanho_composto" in PROMPT,
   "C4 prompt sabe que `instrucao_tamanho_composto` e obrigatoria quando vier")


print("\n" + ("=" * 60))
if pulados:
    print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
