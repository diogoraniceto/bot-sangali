"""Restaura produtos_estoque.embedding a partir de um backup JSON (rollback da virada
de modelo de embedding).

    python restaurar_embeddings.py --backup=migrations/backup/backup_embeddings_001_2026-09-22.json --dry-run
    python restaurar_embeddings.py --backup=... [--limit=N] [--skip=N]

O backup e por `id_unico` (PK), entao a restauracao e EXATA linha a linha — nao
depende da convencao (id_produto, nome) do regenerar. Escrita: update com dict UNICO
por linha (nunca upsert em lote — ver regenerar_embeddings.py, RESTRICOES DURAS).
Recusa vetor com dimensao != 768 (coluna vector(768)). Idempotente; --skip retoma.
Depois de restaurar, o modelo de CONSULTA tem de voltar ao do backup
(GEMINI_EMBEDDING_MODEL / default de embedding_text.py) — senao a busca fica cega
no sentido inverso.
"""
import json, os, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from dotenv import load_dotenv
from supabase import create_client

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
supabase = create_client(os.environ["SUPABASE_URL"], os.environ.get("SUPABASE_KEY") or os.environ["SUPABASE_ANON_KEY"])

def arg(prefixo, default=None):
    for a in sys.argv:
        if a.startswith(prefixo):
            return a.split("=", 1)[1]
    return default

def main():
    caminho = arg("--backup=")
    if not caminho or not os.path.exists(caminho):
        print("uso: --backup=<arquivo.json> [--dry-run] [--limit=N] [--skip=N]"); sys.exit(2)
    dry = "--dry-run" in sys.argv
    skip = int(arg("--skip=", 0)); limite = int(arg("--limit=", 0))
    with open(caminho, encoding="utf-8") as f:
        bk = json.load(f)
    linhas = bk["linhas"]
    print(f"[i] backup: modelo={bk.get('modelo')} dim={bk.get('dim')} gerado_em={bk.get('gerado_em')} linhas={len(linhas)}")
    validas, invalidas = [], 0
    for r in linhas:
        v = r.get("embedding")
        if isinstance(v, str):
            try: v = json.loads(v)
            except Exception: v = None
        if isinstance(v, list) and len(v) == 768 and r.get("id_unico"):
            validas.append((r["id_unico"], v))
        else:
            invalidas += 1
    print(f"[i] restauraveis: {len(validas)} | invalidas/sem vetor: {invalidas}")
    if skip: validas = validas[skip:]
    if limite: validas = validas[:limite]
    print(f"[i] a processar: {len(validas)} (skip={skip} limit={limite or '-'}){' | DRY-RUN' if dry else ''}")
    if dry:
        print("[dry-run] nada foi escrito."); return
    ok = err = 0; t0 = time.time()
    for i, (idu, v) in enumerate(validas, 1):
        try:
            supabase.table("produtos_estoque").update({"embedding": v}).eq("id_unico", idu).execute()
            ok += 1
        except Exception as e:
            err += 1; print(f"  ❌ {idu}: {str(e)[:80]}")
        if i % 200 == 0:
            print(f"  [{skip+i}/{skip+len(validas)}] ok={ok} err={err} {time.time()-t0:.0f}s")
    print(f"FIM: ok={ok} err={err} em {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
