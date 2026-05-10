"""
Script para regerar os embeddings dos produtos usando gemini-embedding-001.
Os embeddings antigos foram gerados com text-embedding-004 (descontinuado).
O novo modelo (gemini-embedding-001) com output_dimensionality=768 gera vetores
compatíveis em dimensão, mas precisamos regerar porque os espaços vetoriais são diferentes.
"""

import os
import time
from dotenv import load_dotenv
import google.generativeai as genai
from supabase import create_client

# Carrega .env da pasta atual ou da subpasta bot-control-panel
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot-control-panel", ".env")
load_dotenv(env_path)
print(f"📂 Carregando .env de: {env_path}")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

genai.configure(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def gerar_embedding(texto):
    """Gera embedding com o novo modelo."""
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=texto,
            task_type="retrieval_document",
            output_dimensionality=768
        )
        return result['embedding']
    except Exception as e:
        print(f"  ❌ Erro: {e}")
        return None

def main():
    import sys as _sys
    only_missing = "--only-missing" in _sys.argv
    print(f"[i] modo: {'somente sem embedding' if only_missing else 'todos os produtos'}")

    # Busca todos com paginação. Se only_missing, filtra embedding IS NULL.
    produtos = []
    page_size = 1000
    offset = 0
    while True:
        q = supabase.table("produtos_estoque").select("id_produto, nome, tamanho, preco")
        if only_missing:
            q = q.is_("embedding", "null")
        response = q.range(offset, offset + page_size - 1).execute()
        batch = response.data
        if not batch:
            break
        produtos.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    # Deduplica por id_produto (cada variação tem mesmo id_produto, embedding é por produto base)
    vistos = set()
    unicos = []
    for p in produtos:
        if p.get("id_produto") in vistos:
            continue
        vistos.add(p.get("id_produto"))
        unicos.append(p)
    produtos = unicos

    print(f"[i] Produtos únicos para gerar embedding: {len(produtos)}")
    print("=" * 60)
    
    atualizados = 0
    erros = 0
    
    for i, produto in enumerate(produtos):
        # Monta o texto para embedding
        partes = []
        if produto.get('nome'):
            partes.append(produto['nome'])
        if produto.get('tamanho'):
            partes.append(f"Tamanho: {produto['tamanho']}")
        
        texto = " | ".join(partes)
        
        print(f"[{i+1}/{len(produtos)}] {produto.get('nome', 'SEM NOME')[:50]}...", end=" ")
        
        embedding = gerar_embedding(texto)
        
        if embedding:
            # Atualiza no banco
            supabase.table("produtos_estoque").update({
                "embedding": embedding
            }).eq("id_produto", produto["id_produto"]).execute()
            
            atualizados += 1
            print("✅")
        else:
            erros += 1
            print("❌")
        
        # Pausa para não estourar rate limit da API (1500 RPM)
        if (i + 1) % 100 == 0:
            print(f"\n⏸️  Pausa de 5s para evitar rate limit... ({i+1}/{len(produtos)})\n")
            time.sleep(5)
    
    print("=" * 60)
    print(f"✅ Atualizados: {atualizados}")
    print(f"❌ Erros: {erros}")
    print(f"📦 Total: {len(produtos)}")
    print("🎉 Pronto! Os embeddings foram regenerados com gemini-embedding-001.")

if __name__ == "__main__":
    main()
