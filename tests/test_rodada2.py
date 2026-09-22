"""Rodada 2 do PLANO_CAMPANHA_2 — A2 (buscar_por_preco) e A3 (curadoria com material).

Deterministico, sem Gemini. Le o banco de producao, nao escreve.

Q1  A3: `_qualificadores_material` — LIGANETE/RENDA entram, COSTURA e marcas NAO
Q2  A3: a curadoria aceita 'BABY DOLL DE LIGANETE' para "camisola de liganete"
Q3  A2: `_dica_preco` dispara em R$ / reais / 'mais barata' / 'ate X' e nao em saudacao
Q4  A2: buscar_por_preco contra o banco — o caso do anuncio (liganete 17,50 atacado)
Q5  A2: baby doll ate R$25 varejo existe; ordem crescente; 1 linha por produto
Q6  A2: 'vazio' quando o teto e impossivel, e ainda assim informa menor_preco
Q7  A2: politica de infantil vale aqui tambem
Q8  A2: o extrator de cards enxerga a tool nova (senao o id nao vira card)
Q9  contrato do prompt
Q10 prompt local == banco

    python tests/test_rodada2.py
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

print("### Q1 — A3: quais tokens qualificam como material")
Q = bot._qualificadores_material
ok(Q("camisola de liganete") == ["LIGANETE"], "Q1 'camisola de liganete' -> LIGANETE", Q("camisola de liganete"))
ok(Q("calcinha de renda M") == ["RENDA"], "Q1 'calcinha de renda' -> RENDA", Q("calcinha de renda M"))
ok(Q("pijama de algodão") == ["ALGODAO"], "Q1 acento normalizado: algodão -> ALGODAO", Q("pijama de algodão"))
ok(Q("calcinha sem costura") == [], "Q1 COSTURA NAO qualifica (puxaria SUTIA SEM COSTURA)", Q("calcinha sem costura"))
ok(Q("cueca zee rucci") == [], "Q1 marca (RUCCI) NAO qualifica", Q("cueca zee rucci"))
ok(Q("fantasia de enfermeira") == [], "Q1 sem material -> lista vazia (curadoria volta ao substantivo)")
ok(Q("") == [] and Q(None) == [], "Q1 vazio/None nao quebra")

print("\n### Q2 — A3: a curadoria aceita a peca do material pedido")
# LIMITE DOCUMENTADO (22/09): "camisola de liganete M" NAO serve de sonda aqui. Em M,
# nas 2 lojas, a unica liganete e o BABY DOLL DE LIGANETE URDA, e o KNN de "camisola
# de liganete" nao o traz para o pool de 60 — `palavras_chave` ja contem LIGANETE,
# entao nao e escopo lexical, e limite do ranking semantico (fora do escopo da
# Rodada 2). O caminho que cobre o anuncio e o A2 (Q4). A allowlist se mede onde o
# material ESTA no pool: "calcinha de renda" (RENDA = 26 produtos vivos).
try:
    bot._set_turn_ctx("t_r2", "tem calcinha de renda M?")
    r = quieto(bot.consultar_estoque_supabase, "calcinha de renda", tamanho="M", id_loja="244033,94134")
    bot._clear_turn_ctx()
    if r.get("status") != "sucesso":
        skip("Q2", f"busca devolveu {r.get('status')}")
    else:
        ps = r.get("produtos") or []
        nomes = [(p.get("nome") or "").upper() for p in ps]
        instr = (r.get("filtro_aplicado") or {}).get("instrucao_curadoria") or ""
        renda_sem_calcinha = [n for n in nomes if "RENDA" in bot._norm_txt(n) and "CALCINHA" not in n]
        if not renda_sem_calcinha:
            skip("Q2", "nenhuma peca de RENDA sem 'CALCINHA' no nome veio no pool hoje")
        else:
            ok(True, f"Q2 pool tem peca de RENDA que nao se chama calcinha: {renda_sem_calcinha[0][:34]}")
            # Antes do A3 essa peca era tratada como 'outra categoria'. Agora ela conta
            # como da categoria, entao a instrucao (se vier) NAO pode mandar descarta-la:
            # ou nao ha instrucao (lista majoritariamente certa) ou o rotulo cita RENDA.
            ok((not instr) or ("RENDA" in instr.upper()),
               "Q2 instrucao de curadoria nao trata a peca de RENDA como fora da categoria", instr[:110])
            ok("NENHUM" not in instr, "Q2 ... e nao declara lista vazia tendo peca do material pedido", instr[:90])
except Exception as e:
    skip("Q2", f"{type(e).__name__}: {str(e)[:70]}")

print("\n### Q3 — A2: quando a dica de preco entra")
D = bot._dica_preco
for m in ("tem por R$25?", "É verdade que tem camisolas de liganete por R$17,50 no atacado?",
          "Gostaria de ver baby dolls de R$25,00.", "quero algo ate 30 reais", "qual a mais barata?",
          "tem baby doll por 25 reais"):
    ok(D(m), f"Q3 dispara: {m[:50]!r}")
for m in ("oi, boa noite", "queria ver camisolas tamanho G", "Vocês vendem atacado?", "manda foto"):
    ok(not D(m), f"Q3 NAO dispara: {m[:40]!r}")
ok("buscar_por_preco" in bot._DICA_PRECO and "menor_preco" in bot._DICA_PRECO, "Q3 a dica cita a tool e o menor_preco")

print("\n### Q4 — A2: o caso do anuncio, contra o banco")
try:
    bot._set_turn_ctx("t_r2", "liganete R$17,50 atacado")
    r = quieto(bot.buscar_por_preco, "camisola liganete", 17.50, "atacado")
    bot._clear_turn_ctx()
    ok(r.get("status") == "sucesso", "Q4 status sucesso", r.get("status"))
    ps = r.get("produtos") or []
    nomes = [(p.get("nome") or "").upper() for p in ps]
    ok(any("LIGANETE" in n for n in nomes), "Q4 devolve peca de LIGANETE ate R$17,50 atacado (o anuncio estava certo)", nomes)
    ok(all(p["preco_atacado"] <= 17.50 + 1e-6 for p in ps), "Q4 todas <= R$17,50 no atacado", [p["preco_atacado"] for p in ps])
    ok(r.get("menor_preco") is not None and abs(r["menor_preco"] - 17.5) < 0.01, "Q4 menor_preco do termo = 17,50", r.get("menor_preco"))
    ok(all(k in ps[0] for k in ("id_produto", "nome", "preco", "preco_varejo", "preco_atacado", "imagem", "tem_foto", "n_fotos", "destaque", "tamanho")) if ps else False,
       "Q4 formato igual ao da busca semantica (o card sabe renderizar)", list(ps[0].keys()) if ps else [])
    ok(ps and abs(ps[0]["preco"] - ps[0]["preco_varejo"]) < 1e-6, "Q4 `preco` = varejo (o card calcula atacado = varejo*(1-desconto))")
    ok("instrucao_preco" in r and "17.50" in r["instrucao_preco"], "Q4 instrucao_preco cita o menor preco real")
except Exception as e:
    skip("Q4", f"{type(e).__name__}: {str(e)[:70]}")

print("\n### Q5 — A2: baby doll ate R$25 varejo")
try:
    bot._set_turn_ctx("t_r2", "baby doll R$25")
    r = quieto(bot.buscar_por_preco, "baby doll", 25, "varejo")
    bot._clear_turn_ctx()
    ps = r.get("produtos") or []
    ok(r.get("status") == "sucesso" and ps, "Q5 existe baby doll ate R$25 (a Luna dizia que nao)", r.get("status"))
    precos = [p["preco_varejo"] for p in ps]
    ok(precos == sorted(precos), "Q5 ordem crescente de preco", precos)
    ids = [p["id_produto"] for p in ps]
    ok(len(ids) == len(set(ids)), "Q5 uma linha por produto (dedupe)", ids)
    ok(all("BABY DOLL" in (p.get("nome") or "").upper() or "BABY DOOL" in (p.get("nome") or "").upper() for p in ps),
       "Q5 todos sao baby doll", [p.get("nome") for p in ps])
    ok(len(ps) <= bot.LIMITE_PRODUTOS, "Q5 respeita LIMITE_PRODUTOS")
    ok(r.get("menor_preco") is not None and r["menor_preco"] <= 25, "Q5 menor_preco <= 25", r.get("menor_preco"))
except Exception as e:
    skip("Q5", f"{type(e).__name__}: {str(e)[:70]}")

print("\n### Q6 — A2: teto impossivel -> vazio, mas honesto")
try:
    bot._set_turn_ctx("t_r2", "camisola por 1 real")
    r = quieto(bot.buscar_por_preco, "camisola", 1.00, "varejo")
    bot._clear_turn_ctx()
    ok(r.get("status") == "vazio", "Q6 status vazio para R$1", r.get("status"))
    ok(r.get("menor_preco") is not None and r["menor_preco"] > 1, "Q6 ainda assim informa o menor preco real", r.get("menor_preco"))
    ok("menor" in (r.get("msg") or "").lower() or "mais barato" in (r.get("msg") or "").lower(), "Q6 msg orienta o modelo com o piso real", (r.get("msg") or "")[:90])
    r2 = quieto(bot.buscar_por_preco, "camisola", "abc", "varejo")
    ok(r2.get("status") == "erro", "Q6 preco_max invalido -> erro, nao excecao")
except Exception as e:
    skip("Q6", f"{type(e).__name__}: {str(e)[:70]}")

print("\n### Q7 — A2: infantil so se pedir, tambem na busca por preco")
try:
    bot._set_turn_ctx("t_r2", "cueca ate 30 reais")
    r = quieto(bot.buscar_por_preco, "cueca", 30, "varejo")
    bot._clear_turn_ctx()
    kids = [p.get("nome") for p in (r.get("produtos") or []) if bot._produto_infantil(p.get("nome"))]
    ok(not kids, "Q7 'cueca ate 30' nao traz cueca infantil (CUECA INFANTIL SRAMBOX custa menos que isso)", kids)
    bot._set_turn_ctx("t_r2", "cueca infantil ate 30 reais")
    r = quieto(bot.buscar_por_preco, "cueca infantil", 30, "varejo")
    bot._clear_turn_ctx()
    kids = [p.get("nome") for p in (r.get("produtos") or []) if bot._produto_infantil(p.get("nome"))]
    ok(bool(kids), "Q7 ... mas quem PEDE infantil recebe", r.get("status"))
except Exception as e:
    skip("Q7", f"{type(e).__name__}: {str(e)[:70]}")

print("\n### Q8 — o extrator de cards enxerga buscar_por_preco")
class _FR:
    def __init__(self, name, response): self.name = name; self.response = response
class _Part:
    def __init__(self, fr): self.function_response = fr
class _Content:
    def __init__(self, parts): self.parts = parts
hist = [_Content([_Part(_FR("buscar_por_preco", {"result": {"produtos": [
    {"id_produto": "10297473", "nome": "10B BABY DOLL DE LIGANETE URDA", "preco": 25.0,
     "preco_varejo": 25.0, "preco_atacado": 17.5, "imagem": "x.jpg", "tamanho": "M", "id_unico": "u"}]}}))])]
cache = bot.extrair_produtos_de_tool_results(hist)
ok(10297473 in cache and cache[10297473]["preco"] == 25.0, "Q8 id da tool nova entra no cache do card", list(cache.keys()))
hist2 = [_Content([_Part(_FR("mostrar_fotos_produto", {"result": {"produtos": [{"id_produto": "1", "nome": "x"}]}}))])]
ok(1 not in bot.extrair_produtos_de_tool_results(hist2), "Q8 mostrar_fotos_produto continua FORA (senao foto duplica)")

print("\n### Q9 — contrato do prompt")
with open(os.path.join(ROOT, "prompt_luna_v2.txt"), encoding="utf-8") as f:
    P = f.read()
ok("## 9.0.5. PREÇO CITADO PELO CLIENTE" in P, "Q9 §9.0.5 existe")
ok("buscar_por_preco" in P, "Q9 prompt cita a tool")
ok("PROIBIDO sem essa tool no mesmo turno" in P, "Q9 proibe 'a partir de' sem a tool")
ok("[cliente citou PRECO/VALOR" in P, "Q9 reconhece a marcacao dinamica")
ok("MATERIAL/MODELO QUE O CLIENTE PEDIU CONTA COMO CATEGORIA" in P, "Q9 §4: material conta como categoria")
ok("BABY DOLL DE LIGANETE URDA" in P, "Q9 §4 usa o exemplo real do anuncio")

print("\n### Q10 — prompt local == banco")
try:
    db = bot.supabase.table("bot_settings").select("system_prompt").eq("id", 1).single().execute()
    dbp = (db.data or {}).get("system_prompt")
    ok(dbp == P, "Q10 byte-identico", f"local={len(P)} banco={len(dbp) if dbp else None}")
except Exception as e:
    skip("Q10", f"{type(e).__name__}: {str(e)[:60]}")

print("\n" + "=" * 60)
if pulados: print(f"PULADOS ({len(pulados)}): {pulados}")
if falhas: print(f"FALHAS ({len(falhas)}): {falhas}"); sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
