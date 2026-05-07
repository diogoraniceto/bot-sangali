"""Script para inserir/atualizar o prompt da Luna no banco.

Lê de prompt_luna_v2.txt por padrão. Sobrescreve `bot_settings.system_prompt`.
"""
import os
import sys
from dotenv import load_dotenv
from supabase import create_client

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot-control-panel", ".env")
load_dotenv(env_path)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

prompt_file = sys.argv[1] if len(sys.argv) > 1 else "prompt_luna_v2.txt"
prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), prompt_file)

with open(prompt_path, "r", encoding="utf-8") as f:
    prompt = f.read()

print(f"[i] Lendo prompt de: {prompt_path} ({len(prompt)} chars)")

result = supabase.table("bot_settings").update({"system_prompt": prompt}).eq("id", 1).execute()
print(f"[ok] Prompt atualizado em bot_settings (id=1). Registros: {len(result.data)}")
