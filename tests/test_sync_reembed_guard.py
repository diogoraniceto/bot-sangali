"""Testes OFFLINE do ciclo de sync (F4): ramo de re-embed, cap por chamadas,
guard de zeragem e contadores do heartbeat.

Zero rede: ERP, Gemini e Supabase sao stubados. Nada e escrito em
produtos_estoque. Complementa tests/test_sync_upsert_homogeneo.py, que prova o
comportamento do postgrest contra o banco real.

    python tests/test_sync_reembed_guard.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["ENABLE_INPROCESS_SYNC"] = "0"
os.environ.setdefault("SUPABASE_URL", "https://stub.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "stub-key")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import sync_erp

falhas = []


def ok(cond, nome, extra=""):
    print(("  [+] " if cond else "  [-] ") + nome + ("" if cond else f"  <-- {extra}"))
    if not cond:
        falhas.append(nome)


# ------------------------------------------------------------------ stubs
class FakeQuery:
    def __init__(self, sink, tabela):
        self.sink = sink
        self.tabela = tabela

    def upsert(self, rows):
        self.sink.append((self.tabela, [dict(r) for r in rows]))
        return self

    def execute(self):
        return self


class FakeClient:
    def __init__(self):
        self.upserts = []

    def table(self, nome):
        return FakeQuery(self.upserts, nome)


class FakeResp:
    def __init__(self, produtos, status=200):
        self.status_code = status
        self._p = produtos

    def json(self):
        return {"data": self._p}


def produto(pid, nome, tamanho, qtd, preco=10.0, grupo="1", nome_grupo="LINGERIE"):
    return {
        "id": pid, "nome": nome, "grupo_id": grupo, "nome_grupo": nome_grupo,
        "valor_venda": preco, "valores": [],
        "variacoes": [{"variacao": {"nome": tamanho, "estoque": qtd,
                                    "valores": [], "valor_venda": preco}}],
    }


def linha_estado(nome, estoque, preco=10.0):
    return {"nome": nome, "estoque": float(estoque), "preco": float(preco),
            "preco_varejo": float(preco), "preco_varejo_avista": float(preco),
            "preco_atacado": float(preco), "preco_atacado_aprazo": float(preco),
            "id_loja": "L1", "grupo_id": "1", "nome_grupo": "LINGERIE"}


def montar(paginas, estado, sem_embedding, cap=300, embeds_ok=True):
    """Prepara sync_erp para uma execucao offline e devolve o FakeClient."""
    cli = FakeClient()
    sync_erp.supabase_client = cli
    sync_erp.embedding_cache = {}
    sync_erp.REEMBED_MAX_POR_CICLO = cap
    sync_erp.get_lojas = lambda: [{"id": "L1", "nome": "MATRIZ"}]
    sync_erp.carregar_estado_atual_do_banco = lambda: dict(estado)
    sync_erp.carregar_ids_sem_embedding = lambda: set(sem_embedding)

    chamadas = []

    def fake_embed(texto):
        if texto in sync_erp.embedding_cache:
            return sync_erp.embedding_cache[texto]
        chamadas.append(texto)
        if not embeds_ok:
            return None
        v = [0.1] * 8
        sync_erp.embedding_cache[texto] = v
        return v

    sync_erp.get_embedding = fake_embed

    seq = list(paginas)

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            i = (params or {}).get("pagina", 1) - 1
            return seq[i] if i < len(seq) else FakeResp([])

    sync_erp.session = FakeSession()
    return cli, chamadas


_orig = {k: getattr(sync_erp, k) for k in
         ("supabase_client", "get_lojas", "carregar_estado_atual_do_banco",
          "carregar_ids_sem_embedding", "get_embedding", "session",
          "REEMBED_MAX_POR_CICLO", "embedding_cache")}

try:
    # ---------------------------------------------------------------- 1
    print("### 1. Ramo de re-embed: linha existente, nome igual, vetor NULL")
    estado = {"L1_p1_M": linha_estado("CAMISOLA X", 5),
              "L1_p2_M": linha_estado("CALCINHA Y", 5)}
    cli, chamadas = montar(
        [FakeResp([produto("p1", "CAMISOLA X", "M", 5),
                   produto("p2", "CALCINHA Y", "M", 5)]), FakeResp([])],
        estado, sem_embedding={"L1_p1_M"})
    sync_erp.sync_otimizado()

    upsertados = {r["id_unico"]: r for _, rows in cli.upserts for r in rows}
    ok("L1_p1_M" in upsertados, "linha com vetor NULL foi para o upsert", list(upsertados))
    ok("embedding" in upsertados.get("L1_p1_M", {}), "e recebeu a chave 'embedding'")
    ok("L1_p2_M" not in upsertados, "linha intacta continua em skipped (nao reescrita)")
    ok(chamadas == ["CAMISOLA X"], "uma unica chamada de embedding, no texto canonico", chamadas)
    ok(sync_erp.LAST_RUN_REEMBEDS == 1, "LAST_RUN_REEMBEDS", sync_erp.LAST_RUN_REEMBEDS)
    ok(sync_erp.LAST_RUN_EMB_NULOS == 1, "LAST_RUN_EMB_NULOS", sync_erp.LAST_RUN_EMB_NULOS)
    ok(sync_erp.LAST_RUN_OK is True, "ciclo completo -> LAST_RUN_OK True")
    ok("emb_nulos_inicio:1" in sync_erp.LAST_RUN_INFO and "reembeds:1" in sync_erp.LAST_RUN_INFO,
       "contadores no LAST_RUN_INFO", sync_erp.LAST_RUN_INFO)

    # ---------------------------------------------------------------- 2
    print("\n### 2. Todo upsert sai HOMOGENEO (produto novo + vitima no mesmo lote)")
    estado = {"L1_p2_M": linha_estado("CALCINHA Y", 5)}      # p1 e NOVO (portador)
    cli, chamadas = montar(
        [FakeResp([produto("p1", "CAMISOLA NOVA", "M", 5),
                   produto("p2", "CALCINHA Y", "M", 5)]), FakeResp([])],
        estado, sem_embedding={"L1_p2_M"})
    # p2 tem vetor NULL: sem cap ele tambem re-embeda. Forcando cap=0 e cache
    # vazio, p2 entra no lote SEM a chave 'embedding' -> lote misto de verdade.
    sync_erp.REEMBED_MAX_POR_CICLO = 0
    sync_erp.sync_otimizado()

    shapes = [{frozenset(r.keys()) for r in rows} for _, rows in cli.upserts]
    ok(all(len(s) == 1 for s in shapes), "cada upsert tem um unico conjunto de chaves", shapes)
    ok(len(cli.upserts) >= 2, "o lote misto virou >=2 chamadas de upsert", len(cli.upserts))
    tem_emb = [r["id_unico"] for _, rows in cli.upserts for r in rows if "embedding" in r]
    sem_emb = [r["id_unico"] for _, rows in cli.upserts for r in rows if "embedding" not in r]
    ok(tem_emb == ["L1_p1_M"], "so o produto novo carrega vetor", tem_emb)
    ok(sem_emb == ["L1_p2_M"], "a vitima vai num lote proprio, sem a coluna embedding", sem_emb)
    ok(chamadas == ["CAMISOLA NOVA"], "cap=0 bloqueou a chamada do re-embed", chamadas)
    ok(sync_erp.LAST_RUN_REEMBEDS == 0, "nenhum reparo contabilizado sob cap=0")

    # ---------------------------------------------------------------- 3
    print("\n### 3. GUARD: pagina do ERP falhando NAO zera estoque")
    estado = {"L1_p1_M": linha_estado("CAMISOLA X", 5),
              "L1_sumido_M": linha_estado("PRODUTO SUMIDO", 7)}
    cli, chamadas = montar([FakeResp([], status=500)], estado, sem_embedding=set())
    sync_erp.sync_otimizado()
    zeragens = [rows for _, rows in cli.upserts if rows and set(rows[0]) == {"id_unico", "estoque"}]
    ok(zeragens == [], "nenhuma zeragem executada", zeragens)
    ok(sync_erp.LAST_RUN_OK is False, "ciclo degradado -> LAST_RUN_OK False (alimenta o alarme)")

    # ---------------------------------------------------------------- 4
    print("\n### 4. Ciclo completo AINDA zera quem sumiu do ERP")
    estado = {"L1_p1_M": linha_estado("CAMISOLA X", 5),
              "L1_sumido_M": linha_estado("PRODUTO SUMIDO", 7)}
    cli, chamadas = montar(
        [FakeResp([produto("p1", "CAMISOLA X", "M", 5)]), FakeResp([])],
        estado, sem_embedding=set())
    sync_erp.sync_otimizado()
    zerados = [r["id_unico"] for _, rows in cli.upserts for r in rows
               if set(r) == {"id_unico", "estoque"}]
    ok(zerados == ["L1_sumido_M"], "so o que sumiu foi zerado", zerados)
    ok(sync_erp.LAST_RUN_OK is True, "ciclo completo -> LAST_RUN_OK True")

    # ---------------------------------------------------------------- 5
    print("\n### 5. get_embedding falhando nao inventa vetor nem quebra o ciclo")
    estado = {"L1_p1_M": linha_estado("CAMISOLA X", 5)}
    cli, chamadas = montar(
        [FakeResp([produto("p1", "CAMISOLA X", "M", 5)]), FakeResp([])],
        estado, sem_embedding={"L1_p1_M"}, embeds_ok=False)
    sync_erp.sync_otimizado()
    regs = [r for _, rows in cli.upserts for r in rows]
    ok(all("embedding" not in r for r in regs), "nenhum registro gravou embedding nulo/falso")
    ok(sync_erp.LAST_RUN_REEMBEDS == 0, "reparo nao contabilizado quando a API falha")
    ok(sync_erp.LAST_RUN_OK is True, "falha de embedding sozinha nao degrada o ciclo")

finally:
    for k, v in _orig.items():
        setattr(sync_erp, k, v)

print("\n" + ("=" * 60))
if falhas:
    print(f"FALHAS ({len(falhas)}): {falhas}")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM ✅")
