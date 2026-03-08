import os
import requests
import time
from supabase import create_client
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

# Configurações API GestãoClick
GESTAO_CLICK_URL = os.getenv("GESTAO_CLICK_URL")
GESTAO_CLICK_TOKEN = os.getenv("GESTAO_CLICK_TOKEN")
GESTAO_CLICK_SECRET = os.getenv("GESTAO_CLICK_SECRET")

HEADERS = {
    "access-token": GESTAO_CLICK_TOKEN,
    "secret-access-token": GESTAO_CLICK_SECRET,
    "Content-Type": "application/json"
}

# Configurações Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_all_product_ids():
    """Busca todos os IDs distintos de produtos na tabela de estoque do Supabase."""
    print("🔄 Buscando IDs de produtos no Supabase...")
    all_ids = set()
    start = 0
    batch_size = 1000
    
    while True:
        response = supabase.table("produtos_estoque").select("id_produto").range(start, start + batch_size - 1).execute()
        data = response.data
        
        if not data:
            break
            
        for item in data:
            if item.get('id_produto'):
                all_ids.add(item['id_produto'])
        
        if len(data) < batch_size:
            break
            
        start += batch_size
        
    print(f"✅ Encontrados {len(all_ids)} produtos únicos para processar.")
    return list(all_ids)

def get_product_images(produto_id):
    """Busca os detalhes do produto na API e extrai as imagens."""
    # Retornando para a URL padrão que funcionou nos testes
    url = f"{GESTAO_CLICK_URL}/{produto_id}"
    
    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            print(f"⚠️ Erro API para ID {produto_id}: Status {response.status_code}")
            return []
            
        data = response.json()
        # A API retorna os dados diretamente em data['data'] => { 'fotos': [...], ... }
        produto_data = data.get('data', {})
        
        # O campo correto verificado é 'fotos', e é uma lista de strings (URLs)
        fotos = produto_data.get('fotos', [])
        
        image_data = []
        if isinstance(fotos, list):
            for item in fotos:
                url_img = None
                url_mini = None
                
                # Se for string direta (como visto nos logs)
                if isinstance(item, str):
                    url_img = item
                    # Tenta construir a url mini baseada no padrão observado no print do usuário
                    # .../nome.ext -> .../mini_nome.ext
                    try:
                        parts = url_img.split('/')
                        filename = parts[-1]
                        base_path = "/".join(parts[:-1])
                        url_mini = f"{base_path}/mini_{filename}"
                    except:
                        pass
                
                # Se for dict (caso a API mude ou varie)
                elif isinstance(item, dict):
                    url_img = item.get('caminho_imagem') or item.get('imagem')
                    url_mini = item.get('caminho_imagem_mini')

                if url_img:
                    image_data.append({
                        "full": url_img,
                        "mini": url_mini
                    })
                    
        return image_data
        
    except Exception as e:
        print(f"❌ Erro ao buscar imagens para ID {produto_id}: {e}")
        return []

def sync_images():
    product_ids = get_all_product_ids()
    total = len(product_ids)
    
    print(f"🚀 Iniciando sincronização de imagens para {total} produtos...")
    
    for i, p_id in enumerate(product_ids):
        # 1. Buscar imagens na API
        images = get_product_images(p_id)
        
        if images:
            try:
                # 2. Limpar imagens antigas desse produto
                supabase.table("produtos_imagens").delete().eq("produto_id", p_id).execute()
                
                # 3. Inserir novas imagens
                data_list = [
                    {
                        "produto_id": p_id, 
                        "imagem_url": img['full'],
                        "imagem_mini_url": img['mini']
                    }
                    for img in images
                ]
                
                if data_list:
                    supabase.table("produtos_imagens").insert(data_list).execute()
                    print(f"[{i+1}/{total}] ID {p_id}: {len(images)} imagens salvas.")
            except Exception as e:
                print(f"❌ Erro ao salvar no banco para ID {p_id}: {e}")
        else:
            print(f"[{i+1}/{total}] ID {p_id}: Nenhuma imagem encontrada.")
            
        # Pequeno delay para não sobrecarregar a API
        time.sleep(0.1)

if __name__ == "__main__":
    sync_images()
