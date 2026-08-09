"""Convencao CANONICA do texto de embedding de produto — fonte unica de verdade.

Existe um unico lugar que decide "que string vai para o modelo de embedding".
Antes desta frente havia dois, e eles DIVERGIAM: `sync_erp.py` embedava o nome
puro e `regenerar_embeddings.py` embedava `"<nome> | Tamanho: <tamanho>"`. A base
ficou com convencao mista (~40% contaminada, similaridade 0,857-0,930 contra o
canonico) e o ranking de similaridade passou a depender de qual script escreveu
a linha por ultimo.

REGRA: o texto e SOMENTE o nome do produto. Sem tamanho, sem preco, sem loja.

Por que sem tamanho:
  (i)   o filtro de tamanho e resolvido em SQL (`tamanho_tokens` + trigger da
        migration 0001 + a RPC), nunca por similaridade de vetor;
  (ii)  o vetor e por PRODUTO, e as linhas P/M/G de um mesmo id_produto
        compartilham o mesmo vetor. Gravar "| Tamanho: GG" nas 3 linhas escreve
        um vetor que MENTE sobre duas delas (verificado: PIJAMA NADADOR com P, M
        e GG casando 1,0000 com o texto de GG);
  (iii) o lado de CONSULTA (bot.get_embedding, task_type=retrieval_query) nunca
        acrescenta sufixo de tamanho — asimetria entre indexacao e consulta e
        perda de recall pura.

Por que so `.strip()` e nada mais:
  - `.strip()` e no-op medido na base (0 linhas com espaco em borda), entao e
    seguro: nao invalida nenhum vetor ja escrito.
  - NAO normalizar espaco interno (103 linhas com espaco duplo): colapsar
    espaco mudaria o texto de 103 linhas e invalidaria todo vetor ja escrito
    pelo sync em troca de ganho nulo. Esta no backlog (B13), de proposito.

Quem consome: `sync_erp.py` (indexacao incremental) e `regenerar_embeddings.py`
(re-embed em massa). Qualquer caminho novo de escrita de embedding TEM de passar
por aqui — nao reescreva a regra no chamador.
"""

# Modelo e dimensao usados na ESCRITA do vetor (produtos_estoque.embedding e
# vector(768), e o indice HNSW produtos_estoque_embedding_idx depende disso).
MODELO_EMBEDDING = "models/gemini-embedding-001"
DIM_EMBEDDING = 768


def texto_embedding_produto(nome):
    """Texto canonico a embedar para um produto. Vazio => nao embedar.

    >>> texto_embedding_produto("10BR BABY DOOL DE RENDA")
    '10BR BABY DOOL DE RENDA'
    >>> texto_embedding_produto(None)
    ''
    """
    return (nome or "").strip()
