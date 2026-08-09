"""Cache local dos vetores de busca usados por `tests/test_ranking_comercial.py`.

Por que existe: os asserts A1-A8 rodam 7 termos x 2 lojas. Gerar o embedding a
cada execucao custa 14 chamadas ao Gemini e — pior — torna o teste NAO
deterministico (o vetor muda de versao para versao do modelo e o ranking muda
junto). O cache congela o vetor em `tests/vetores_ranking.json`.

DE ONDE VEM O VETOR GRAVADO HOJE (importa para ler os numeros):
  Na rodada da F5 a chave do Gemini estava com o teto mensal de gasto estourado
  ("429 ... exceeded its monthly spending cap"), entao `get_embedding` devolvia
  None e nenhum vetor de CONSULTA pode ser gerado. O cache foi preenchido com um
  PROXY: o CENTROIDE dos embeddings ja gravados em `produtos_estoque` para os
  produtos daquela categoria (`avg(embedding)` no Postgres). E um vetor real, no
  mesmo espaco, no meio do cluster da categoria — serve para provar dedupe,
  escopo de categoria, tiers e exclusoes, que e o que os asserts medem.
  O que ele NAO reproduz fielmente: a escala ABSOLUTA de similaridade. O
  centroide fica mais perto do cluster do que uma frase digitada pelo cliente
  (sim ~0,88-0,97 aqui contra ~0,65-0,85 medido com o Gemini), o que desloca
  `sim_max` e portanto a calibragem de `JANELA_SIMILARIDADE`.

  => Quando a cota do Gemini voltar, REGERAR com vetores de verdade e reconferir
     A5 (quantos pares tem campeao de venda) e a janela:
         .venv/Scripts/python.exe tests/_vetores_ranking.py --regerar
     `--regerar` so usa o Gemini; se ele falhar, o arquivo antigo e preservado.
"""
import json
import os
import sys

_AQUI = os.path.dirname(os.path.abspath(__file__))
ARQ_CACHE = os.path.join(_AQUI, "vetores_ranking.json")

# 7 termos x 2 lojas = os 14 pares que o plano mede.
TERMOS = [
    "baby doll",
    "camisola",
    "pijama masculino",
    "calcinha",
    "sutia",
    "fantasia",
    "cueca boxer",
]
LOJAS = {"MATRIZ": "244033", "FILIAL01": "94134"}


def carregar():
    """dict {termo: vetor}. So le do disco — nunca chama o Gemini.

    Um teste que depende de rede externa para produzir o vetor deixa de ser um
    teste de ranking e vira um teste de cota de API.
    """
    if not os.path.exists(ARQ_CACHE):
        raise RuntimeError(
            f"{ARQ_CACHE} nao existe. Rode: python tests/_vetores_ranking.py --regerar")
    with open(ARQ_CACHE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    faltando = [t for t in TERMOS if t not in cache]
    if faltando:
        raise RuntimeError(f"vetores faltando no cache: {faltando}")
    return cache


def _regerar_com_gemini():
    os.environ.setdefault("ENABLE_INPROCESS_SYNC", "0")
    sys.path.insert(0, os.path.dirname(_AQUI))
    import bot  # noqa: E402
    novo = {}
    for t in TERMOS:
        v = bot.get_embedding(t)
        if not v:
            raise RuntimeError(
                f"Gemini nao devolveu embedding para {t!r} (cota?). "
                f"Cache ANTIGO preservado — nada foi sobrescrito.")
        novo[t] = v
    with open(ARQ_CACHE, "w", encoding="utf-8") as f:
        json.dump(novo, f)
    print(f"[vetores] regerados {len(novo)} com o Gemini (vetores de CONSULTA reais)")


if __name__ == "__main__":
    if "--regerar" in sys.argv:
        _regerar_com_gemini()
    c = carregar()
    print(f"[vetores] {len(c)} termos em {ARQ_CACHE} (dim={len(c[TERMOS[0]])})")
