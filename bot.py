import os
import re
import json
import requests
import time
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPICallError
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from sync_erp import sync_otimizado
from sync_images import sync_images

# ================= CONFIGURAÇÕES =================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
UAZAPI_URL = os.getenv("UAZAPI_URL")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN")

app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

message_buffers = {}

# ================= AUXILIARES DE INTELIGÊNCIA =================

def get_embedding(text):
    """Gera o vetor semântico para a busca no banco."""
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=text,
            task_type="retrieval_query",
            output_dimensionality=768
        )
        return result['embedding']
    except Exception as e:
        print(f"❌ Erro Embedding: {e}")
        return None

# ================= PERSISTÊNCIA (SUPABASE) =================

def save_message(user_id, role, content):
    supabase.table("chat_history").insert({
        "user_id": user_id,
        "role": role,
        "content": content
    }).execute()

def get_history(user_id, limit=10):
    response = supabase.table("chat_history") \
        .select("role, content") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(limit) \
        .execute()
    
    history = []
    for msg in reversed(response.data):
        history.append({"role": msg["role"], "parts": [msg["content"]]})
    return history

# ================= NOVA FERRAMENTA DE BUSCA (SUPABASE) =================

def consultar_estoque_supabase(termo_cliente: str, tamanho: str = None):
    """
    Realiza busca semântica no Supabase usando Embeddings.
    Filtra por tamanho e realiza curadoria de preços.
    """
    print(f"\n[SEMÂNTICO] Buscando: '{termo_cliente}' | Tamanho: {tamanho}")
    
    # 0. Normalização do Tamanho (Agora feita no início para enviar ao banco)
    tamanho_alvo = tamanho.upper().strip() if tamanho else None

    # 1. Gera o vetor da pergunta do cliente
    vetor_busca = get_embedding(termo_cliente)
    if not vetor_busca:
        return {"status": "erro", "msg": "Falha na geração do vetor de busca."}

    # 2. Chama a RPC 'buscar_produtos_semantico' no Supabase
    try:
        rpc_params = {
            'query_embedding': vetor_busca,
            'match_threshold': 0.4, # Sensibilidade da similaridade
            'match_count': 50,      # Trazemos 50 candidatos (já filtrados por tamanho no banco)
            'filtro_tamanho': tamanho_alvo # <--- PRE-FILTERING: O Banco já filtra o tamanho!
        }
        response = supabase.rpc('buscar_produtos_semantico', rpc_params).execute()
        produtos_candidatos = response.data

        # OTIMIZAÇÃO: Remove o vetor de embedding (muito grande e inútil para a LLM) para economizar tokens
        for p in produtos_candidatos:
            p.pop('embedding', None)

        # 2.1. HYBRID SEARCH (KEYWORD BOOSTING)
        # Prioriza produtos que tenham a palavra exata no nome (ex: "RENDA")
        try:
            # Normalização de Plurais (Simples): Adiciona versão sem "S" final
            palavras_originais = [palavra.upper() for palavra in termo_cliente.split() if len(palavra) > 2]
            palavras_chave = set()
            for p in palavras_originais:
                palavras_chave.add(p)
                if p.endswith('S'):
                    palavras_chave.add(p[:-1]) # "CUECAS" -> "CUECA"

            for p in produtos_candidatos:
                score_boost = 0
                nome_prod = p.get('nome', '').upper()
                for termo in palavras_chave:
                    if termo in nome_prod:
                        score_boost += 1
                p['_score_boost'] = score_boost
            
            # Ordena: Quem tem mais palavras chave vai pro topo. 
            # O Python's sort é estável, então se empatar no boost, mantém a ordem original (semântica)
            produtos_candidatos.sort(key=lambda x: x['_score_boost'], reverse=True)
            print(f"[HYBRID] Reordenado! Palavras-chave usadas: {palavras_chave}")

        except Exception as e:
            print(f"⚠️ Erro no Hybrid Boosting: {e}")

        # 2.2. BUSCA DE IMAGENS (NOVO)
        # Para cada produto candidato, buscar a imagem associada
        ids_candidatos = [p['id_produto'] for p in produtos_candidatos]
        if ids_candidatos:
            try:
                # Busca imagens onde produto_id está na lista de candidatos
                # Traz apenas 1 imagem por produto por enquanto (ou todas e a gente filtra)
                # O ideal seria um dicionário {id_produto: url}
                res_imgs = supabase.table("produtos_imagens").select("produto_id, imagem_url, imagem_mini_url").in_("produto_id", ids_candidatos).execute()
                mapa_imagens = {}
                for img in res_imgs.data:
                    pid = str(img['produto_id']) # Force string key
                    # Prioriza imagem original, depois mini. E apenas a primeira encontrada para cada produto
                    if pid not in mapa_imagens:
                        mapa_imagens[pid] = img.get('imagem_url') or img.get('imagem_mini_url')
                
                # Anexa a imagem ao objeto do produto
                for p in produtos_candidatos:
                    p['imagem'] = mapa_imagens.get(str(p['id_produto'])) # Force string lookup
            except Exception as e:
                print(f"⚠️ Erro ao buscar imagens: {e}")
        
        print(f"\n--- DEBUG: TOP {len(produtos_candidatos)} SEMELHANÇAS ENCONTRADAS ---")
        try:
            with open("matches.txt", "w", encoding="utf-8") as f:
                f.write(f"--- DEBUG: TOP {len(produtos_candidatos)} SEMELHANÇAS ENCONTRADAS ---\n")
                for i, p in enumerate(produtos_candidatos):
                    line = f"{i+1}. {p.get('nome')} | Tamanho: {p.get('tamanho')} | Preço: {p.get('preco')}"
                    print(line)
                    f.write(line + "\n")
                f.write("----------------------------------------------------------------\n")
        except Exception as e:
            print(f"Erro ao gravar log: {e}")
        print("----------------------------------------------------------------\n")
    except Exception as e:
        print(f"❌ Erro RPC: {e}")
        return {"status": "erro", "msg": "Erro ao consultar banco de dados."}

    # 3. Filtragem por Tamanho (REDUNDÂNCIA / SEGURANÇA)
    # Mesmo com o banco filtrando, mantemos isso para garantir ou caso a RPC falhe silenciosamente no filtro
    validados = []

    for p in produtos_candidatos:
        # Se o cliente pediu tamanho, validamos se o produto bate
        if tamanho_alvo:
            if p['tamanho'].upper() == tamanho_alvo:
                validados.append(p)
        else:
            # Para cosméticos/acessórios, aceitamos o que vier com maior similaridade
            validados.append(p)

    if not validados:
        return {"status": "vazio", "msg": f"Não encontrei nada disponível no tamanho {tamanho_alvo}."}

    # 4. Curadoria Final (DELEGADA PARA A IA)
    # Retornamos todo o TOP 50 para a IA ter o máximo de opções para escolher
    selecao = validados[:50]
    
    # Deduplica (caso raro de duplicatas no banco)
    selecao = [dict(t) for t in {tuple(d.items()) for d in selecao}]

    print(f"[SEMÂNTICO] Sucesso! Retornando {len(selecao)} itens selecionados.")
    # DEBUG: Verificar se imagens estão indo para a IA
    for s in selecao:
        print(f"DEBUG TOOL: Produto {s['id_produto']} - Imagem: {s.get('imagem')}")
    
    return {"status": "sucesso", "produtos": selecao}

# ================= CONFIGURAÇÃO DA IA (MODELO) =================

# O modelo será inicializado dinamicamente dentro da função process_and_respond
# model = genai.GenerativeModel(...)

# ================= LÓGICA DA CONVERSA & WEBHOOK =================

def process_and_respond(user_id):
    buffer = message_buffers.get(user_id)
    if not buffer: return
    texto_completo = buffer['text']
    print(f"DEBUG INPUT: '{texto_completo}'")
    
    save_message(user_id, "user", texto_completo)

    try:
        # --- BUSCA CONFIGURAÇÃO NO BANCO ---
        # A cada mensagem, verificamos se o bot está ativo e qual o prompt atual.
        try:
             config = supabase.table("bot_settings").select("*").eq("id", 1).single().execute()
        except Exception as e_config:
             print(f"⚠️ Erro ao buscar configs: {e_config}")
             config = None
        
        # Se não conseguir ler config ou is_active for False, ignora/aborta.
        # Ajuste: Se config for None (tabela não existe ou erro), assumimos INATIVO por segurança ou defina um fallback?
        # O prompt pede: "Se is_active for falso, o backend deve ignorar novas mensagens."
        
        if not config or not config.data:
            print("⚠️ Configuração não encontrada. Bot inativo.")
            return

        settings = config.data
        if not settings.get('is_active'):
             print(f"⏹️ Bot desligado no painel para usuário. Ignorando mensagem.")
             return

        system_instruction_dinamica = settings.get('system_prompt', '')
        
        if not system_instruction_dinamica:
             # Fallback caso o prompt esteja vazio no banco (opcional, mas bom pra evitar crash)
             system_instruction_dinamica = "Você é um assistente útil."
        
        # Instancia o modelo com a instrução ATUALIZADA
        model = genai.GenerativeModel(
            model_name='gemini-3-flash-preview',
            tools=[consultar_estoque_supabase],
            system_instruction=system_instruction_dinamica
        )

        history = get_history(user_id)
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        response = chat.send_message(texto_completo)
        resposta_texto = response.text
        print(f"DEBUG OUTPUT: '{resposta_texto}'")

        # DEBUG: Salva resposta crua em arquivo para análise
        try:
            with open("bot_response_debug.txt", "w", encoding="utf-8") as f:
                f.write(resposta_texto)
        except:
            pass

        save_message(user_id, "model", resposta_texto)
        
        # NOVA LÓGICA: Split para separar textos e imagens de forma linear e robusta.
        # O padrão captura a URL no grupo (parenteses), fazendo com que o split retorne:
        # [Texto1, URL1, Texto2, URL2, Texto3...]
        parts = re.split(r"\[IMAGEM:(.*?)\]", resposta_texto, flags=re.DOTALL)
        
        print(f"DEBUG SPLIT: Encontradas {len(parts)} partes na resposta.")

        if len(parts) > 1:
            # Iteramos de 2 em 2: (Texto anterior, URL da imagem)
            # O último elemento sobra (texto após a última imagem)
            for i in range(0, len(parts) - 1, 2):
                texto_legenda = parts[i].strip()
                url_imagem = parts[i+1].strip()
                
                if url_imagem:
                    print(f"📸 Enviando Imagem {i//2 + 1}: Legenda='{texto_legenda[:30]}...' | Url='{url_imagem}'")
                    enviar_midia_whatsapp(user_id, url_imagem, texto_legenda)
                    time.sleep(1.5) # Delay essencial para garantir a ordem no WhatsApp
            
            # Verifica se sobrou texto após a última imagem
            ultimo_texto = parts[-1].strip()
            if ultimo_texto:
                print(f"💬 Enviando texto final: '{ultimo_texto[:30]}...'")
                enviar_mensagem_whatsapp(user_id, ultimo_texto)
        else:
            # Caso sem imagens, envia texto normal
            print("DEBUG: Nenhuma tag de imagem encontrada. Enviando texto único.")
            enviar_mensagem_whatsapp(user_id, resposta_texto)

    except Exception as e:
        print(f"Erro IA: {e}")
    
    del message_buffers[user_id]

def enviar_midia_whatsapp(numero, url_midia, legenda):
    """Envia mídia (imagem/pdf) via UazAPI."""
    # Deriva a URL de mídia baseada na URL de texto configurada no ENV
    # Ex: .../send/text -> .../send/media
    if UAZAPI_URL and "send/text" in UAZAPI_URL:
        url = UAZAPI_URL.replace("send/text", "send/media")
    else:
        # Fallback ou se a URL for diferente
        url = "https://vennx.uazapi.com/send/media"
    
    headers = {
        "token": UAZAPI_TOKEN, 
        "Content-Type": "application/json"
    }
    
    payload = {
        "number": numero,
        "type": "image", # Assumindo imagem por enquanto
        "file": url_midia,
        "docName": "foto_produto.jpg",
        "text": legenda # Legenda vai no campo text conforme documentação
    }
    
    try:
        print(f"📸 Enviando Mídia para {numero}: {url_midia}")
        response = requests.post(url, json=payload, headers=headers)
        print(f"Status Mídia: {response.status_code} | {response.text}")
    except Exception as e:
        print(f"Erro de conexão Uazapi Media: {e}")

def enviar_mensagem_whatsapp(numero, texto):
    # Se o texto contém tags de imagem, não deveria usar essa função, mas vamos manter como fallback
    headers = {"token": UAZAPI_TOKEN, "Content-Type": "application/json"}
    payload = {"number": numero, "text": texto}
    try:
        requests.post(UAZAPI_URL, json=payload, headers=headers)
    except Exception as e:
        print(f"Erro de conexão Uazapi: {e}")

@app.route('/webhook', methods=['POST'])
@app.route('/webhook/<evento>/<tipo>', methods=['POST'])
def webhook(evento=None, tipo=None):
    data = request.json
    msg_data = data.get('message', {})
    if msg_data.get('fromMe'): return jsonify({"status": "ignored"}), 200

    chat_id = msg_data.get('chatid')
    raw_text = msg_data.get('content')

    if chat_id and isinstance(raw_text, str):
        user_id = chat_id.split('@')[0]
        if user_id not in message_buffers:
            message_buffers[user_id] = {"text": raw_text, "timer": None}
        else:
            message_buffers[user_id]["text"] += f" {raw_text}"
            if message_buffers[user_id]["timer"]: message_buffers[user_id]["timer"].cancel()

        t = threading.Timer(10.0, process_and_respond, args=[user_id])
        message_buffers[user_id]["timer"] = t
        t.start()

    return jsonify({"status": "buffering"}), 200

if __name__ == '__main__':
    # Inicia os schedulers de sincronização
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_otimizado, 'interval', minutes=5, misfire_grace_time=120)
    scheduler.add_job(sync_images, 'cron', hour=9, minute=0, misfire_grace_time=3600)
    scheduler.start()
    print("⏰ Schedulers iniciados: ERP (5 min) | Imagens (diário 6h BRT)")

    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)