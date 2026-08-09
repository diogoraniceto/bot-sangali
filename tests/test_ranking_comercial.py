"""F5 — invariantes do ranking comercial (A1-A8).

Bate no Supabase REAL (a RPC 0013 e o objeto sob teste), mas NAO no Gemini: o
vetor de busca vem congelado de `tests/_vetores_ranking.py`. Envio de WhatsApp e
insert de observabilidade sao stubbed.

    .venv/Scripts/python.exe tests/test_ranking_comercial.py

Pre-requisito: migration 0013 aplicada. Sem ela `_rpc_busca` degrada para os 5
args legados e A2/A5/A7 caem — de proposito, e o sinal de deploy invertido.
"""
import os
import statistics
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["ENABLE_INPROCESS_SYNC"] = "0"

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

import bot  # noqa: E402
from _vetores_ranking import carregar, TERMOS, LOJAS  # noqa: E402

VETORES = carregar()

# O teste nao mede o Gemini nem a rede de envio.
bot.get_embedding = lambda texto: VETORES.get(texto)
bot.enviar_mensagem_whatsapp = lambda n, t: {"messageid": "FAKE"}
bot.enviar_midia_whatsapp = lambda n, u, c: {"messageid": "FAKE"}
bot._log_filtro_evento = lambda **k: None

# Campos que a F5 tira do payload: sao internos do ranking (ou rotulo do ERP) e
# custam token em TODO turno de busca.
CAMPOS_PROIBIDOS = ('id_loja', 'loja', 'grupo_id', 'nome_grupo', 'similarity',
                    'tier', '_score_boost', 'estoque_grade', 'n_tamanhos', 'embedding')
CAMPOS_OBRIGATORIOS = ('id_produto', 'id_unico', 'nome', 'tamanho', 'preco',
                       'preco_varejo', 'preco_atacado', 'estoque', 'destaque',
                       'tem_foto', 'n_fotos')

falhas = []


def check(nome, ok, detalhe=""):
    print(f"  [{'PASS' if ok else 'FALHA'}] {nome}{(' — ' + detalhe) if detalhe else ''}")
    if not ok:
        falhas.append(f"{nome}: {detalhe}")


def head_noun(nome):
    """1a palavra do nome, sem acento, em maiuscula — o mesmo que a RPC faz com
    split_part(norm_busca(nome),' ',1). 99,1% dos produtos com estoque comecam
    pelo substantivo da categoria (so 11 de 1.275 abrem com codigo)."""
    s = ''.join(c for c in unicodedata.normalize('NFD', nome or '')
                if unicodedata.category(c) != 'Mn')
    partes = s.upper().split()
    return partes[0] if partes else ''


def buscar(termo, id_loja, tamanho=None, user_id="eval_ranking", excluir=None):
    bot._set_turn_ctx(user_id, f"tem {termo}?")
    if excluir is not None:
        bot._turn_ctx.excluir_ids = list(excluir)
    try:
        return bot.consultar_estoque_supabase(termo, tamanho, id_loja)
    finally:
        bot._clear_turn_ctx()


def main():
    pares = [(t, ln, lid) for t in TERMOS for ln, lid in LOJAS.items()]
    resultados = {}

    print("\n=== A1: a tool responde 'sucesso' nos 14 pares ===")
    for termo, ln, lid in pares:
        r = buscar(termo, lid)
        resultados[(termo, ln)] = r
        check(f"A1 {termo}/{ln}", r.get("status") == "sucesso", r.get("status"))

    print("\n=== A2: <= LIMITE_PRODUTOS itens, todos com id_produto DISTINTO ===")
    for (termo, ln), r in resultados.items():
        prods = [p.get('id_produto') for p in r.get('produtos', [])]
        check(f"A2 {termo}/{ln}",
              len(prods) <= bot.LIMITE_PRODUTOS and len(set(prods)) == len(prods),
              f"{len(prods)} linhas / {len(set(prods))} distintos")

    print("\n=== A3: nenhum campo interno no payload; os uteis estao todos la ===")
    for (termo, ln), r in resultados.items():
        vazou, faltou = set(), set()
        for p in r.get('produtos', []):
            vazou |= {k for k in CAMPOS_PROIBIDOS if k in p}
            faltou |= {k for k in CAMPOS_OBRIGATORIOS if k not in p}
        check(f"A3 {termo}/{ln}", not vazou and not faltou,
              f"vazou={sorted(vazou)} faltou={sorted(faltou)}")

    print("\n=== A4: o #1 de similaridade pura esta entre os ANCORA_SEMANTICA primeiros ===")
    # Nao "e o primeiro": o desempate por palavra-chave reordena DENTRO do tier 0,
    # de proposito. O que nao pode e a ancora sumir do topo.
    for termo, ln, lid in pares:
        r = resultados[(termo, ln)]
        prods = r.get('produtos', [])
        if not prods:
            continue
        # o topo por similaridade pura e o 1o item da RPC sem re-sort do Python:
        # reproduzido chamando a RPC direto com os mesmos parametros.
        resp = bot._rpc_busca({
            'query_embedding': VETORES[termo], 'match_threshold': 0.5,
            'match_count': bot.POOL_CANDIDATOS, 'filtro_tamanho': None,
            'filtro_id_loja': lid, 'limite_produtos': bot.LIMITE_PRODUTOS,
            'ancora_semantica': bot.ANCORA_SEMANTICA,
            'janela_similaridade': bot.JANELA_SIMILARIDADE,
            'minimo_grade': bot.RANKING_MINIMO_GRADE,
            'termo_tokens': None, 'excluir_ids': None})
        linhas = resp.data or []
        top_sim = max(linhas, key=lambda x: x.get('similarity') or 0)['id_produto'] if linhas else None
        primeiros = [p['id_produto'] for p in prods[:bot.ANCORA_SEMANTICA]]
        check(f"A4 {termo}/{ln}", top_sim in primeiros,
              f"top_sim={top_sim} primeiros={primeiros}")

    print("\n=== A5: INVARIANTE — campeao elegivel e SEMPRE promovido (e so ele) ===")
    # POR QUE NAO E UMA CONTAGEM DE PARES:
    # "quantos pares tem destaque" e propriedade do CATALOGO, nao do ranking.
    # Medido com vetores de consulta REAIS do Gemini: dos 14 pares, so 11 tem
    # campeao de venda DENTRO da categoria pedida — `fantasia` nas 2 lojas nao tem
    # nenhuma campea, e `sutia`/FILIAL01 so tem CONJUNTO e CALCINHA, que o escopo
    # lexical barra CERTO. Desses 11, 9 caem na janela de 0,05; os 2 de fora sao
    # `cueca boxer`, com delta 0,1516 (sim_max 0,8455 contra campeao 0,6939) —
    # existe uma cueca MUITO mais parecida com o pedido, e promover a campea ali
    # seria o defeito que o plano alerta ("estoque antes de similaridade puxa
    # categoria adjacente"). Alargar a janela de 0,05 para 0,15 nao muda nada
    # (segue 9/11); so 0,20 cobriria — e 0,20 sobre uma faixa de 0,65-0,85
    # praticamente desliga a trava. Ou seja: 0,05 esta calibrado.
    # O QUE O CODIGO DEVE GARANTIR, e o que este assert mede, e o invariante nas
    # DUAS direcoes, usando a propria RPC como fonte da elegibilidade (tier=1),
    # sem reimplementar a regra em Python.
    com_destaque = []
    for termo, ln, lid in pares:
        r = resultados[(termo, ln)]
        tem_destaque = any(p.get('destaque') for p in r.get('produtos', []))
        if tem_destaque:
            com_destaque.append(f"{termo}/{ln}")
        # Pool inteiro ranqueado pela RPC, sem ancora (para tier 1 nao ser
        # mascarado por tier 0) e sem corte: quem sai com tier=1 aqui e, pela
        # definicao da propria funcao, campeao elegivel na categoria.
        resp = bot._rpc_busca({
            'query_embedding': VETORES[termo], 'match_threshold': 0.5,
            'match_count': bot.POOL_CANDIDATOS, 'filtro_tamanho': None,
            'filtro_id_loja': lid, 'limite_produtos': bot.POOL_CANDIDATOS,
            'ancora_semantica': 0,
            'janela_similaridade': bot.JANELA_SIMILARIDADE,
            'minimo_grade': bot.RANKING_MINIMO_GRADE,
            'termo_tokens': [t.upper() for t in termo.split()], 'excluir_ids': None})
        elegiveis = [x for x in (resp.data or []) if x.get('tier') == 1]
        if elegiveis:
            check(f"A5 {termo}/{ln}: existe campeao elegivel -> foi promovido",
                  tem_destaque,
                  f"{len(elegiveis)} elegivel(is), ex.: {elegiveis[0]['nome'][:40]}")
        else:
            check(f"A5 {termo}/{ln}: sem campeao elegivel -> nao inventa destaque",
                  not tem_destaque,
                  "pool nao tem tier=1")
    print(f"  [info] pares com destaque: {len(com_destaque)}/{len(pares)} -> {com_destaque}")

    print("\n=== A6: mediana de produtos distintos por busca ===")
    med = statistics.median([len({p['id_produto'] for p in r.get('produtos', [])})
                             for r in resultados.values()])
    check("A6 mediana == LIMITE_PRODUTOS", med == bot.LIMITE_PRODUTOS, f"mediana={med}")

    print("\n=== A7: excluir_ids nao repete nada e ainda devolve >= 5 ===")
    for termo, ln, lid in pares:
        primeiros = [p['id_produto'] for p in resultados[(termo, ln)].get('produtos', [])]
        if not primeiros:
            continue
        r2 = buscar(termo, lid, excluir=primeiros)
        segundos = [p['id_produto'] for p in r2.get('produtos', [])]
        repetidos = set(primeiros) & set(segundos)
        aplicados = r2.get('filtro_aplicado', {}).get('excluir_ids_aplicados')
        # aplicados == 0 => o fallback abandonou as exclusoes (pool esgotado); ai
        # repetir e o comportamento CORRETO — o errado seria devolver vazio.
        if aplicados:
            check(f"A7 {termo}/{ln}", not repetidos and len(segundos) >= 5,
                  f"{len(segundos)} ineditos, repetidos={repetidos}")
        else:
            check(f"A7 {termo}/{ln} (fallback)", len(segundos) >= 1,
                  f"fallback sem exclusoes, {len(segundos)} itens")

    print("\n=== A8: nenhum tier 1 fora da categoria do #1 (o defeito do fantasia+G) ===")
    # tier/destaque saem do payload, entao a checagem le a RPC direto — e o
    # invariante do SQL, nao do dict que vai ao Gemini.
    for termo, ln, lid in pares:
        for tam in (None, 'G'):
            resp = bot._rpc_busca({
                'query_embedding': VETORES[termo], 'match_threshold': 0.5,
                'match_count': bot.POOL_CANDIDATOS, 'filtro_tamanho': tam,
                'filtro_id_loja': lid, 'limite_produtos': bot.LIMITE_PRODUTOS,
                'ancora_semantica': bot.ANCORA_SEMANTICA,
                'janela_similaridade': bot.JANELA_SIMILARIDADE,
                'minimo_grade': bot.RANKING_MINIMO_GRADE,
                'termo_tokens': sorted({w.upper() for w in termo.split() if len(w) > 2}),
                'excluir_ids': None})
            linhas = resp.data or []
            if not linhas:
                continue
            h1 = head_noun(linhas[0]['nome'])
            tokens = {w.upper() for w in termo.split() if len(w) > 2}
            maus = [l['nome'] for l in linhas if l.get('tier') == 1
                    and head_noun(l['nome']) != h1
                    and not any(t in head_noun(l['nome']) or t in (l['nome'] or '').upper()
                                for t in tokens)]
            check(f"A8 {termo}/{ln}/tam={tam}", not maus, f"{maus}")

    print("\n=== A9: exclusoes NAO valem para consultar_produto_por_id ===")
    # Restricao dura: 'manda de novo aquele' tem de funcionar mesmo com o produto
    # ja mostrado. A tool le a tabela direto, sem passar pela RPC — este assert
    # existe para que ninguem "unifique" os dois caminhos depois.
    alvo = None
    for r in resultados.values():
        if r.get('produtos'):
            alvo = r['produtos'][0]['id_produto']
            break
    bot._set_turn_ctx("eval_ranking", "manda de novo aquele")
    bot._turn_ctx.excluir_ids = [str(alvo)]
    try:
        r = bot.consultar_produto_por_id(int(alvo))
    finally:
        bot._clear_turn_ctx()
    check("A9 consultar_produto_por_id ignora excluir_ids",
          r.get('status') == 'sucesso' and str(r.get('produto', {}).get('id_produto')) == str(alvo),
          f"status={r.get('status')} id={r.get('produto', {}).get('id_produto')} (excluido={alvo})")

    print("\n=== A11: fallback quando as exclusoes esvaziam o resultado ===")
    # Nao da para provocar com dado real: o pool sao os 60 vizinhos, entao excluir
    # os 40 produtos que ele alcanca so faz o pool descer para os 60 seguintes
    # (verificado: excluir 40/40 ainda devolve 8). Por isso o teste e dirigido —
    # a RPC e stubada para devolver 1 linha com exclusoes e 6 sem.
    class _Fake:
        def __init__(self, d):
            self.data = d

    def _linha(i, pid):
        return {"id_unico": f"u{i}", "id_produto": str(pid), "nome": f"CALCINHA {i}",
                "tamanho": "M", "preco": 10, "preco_varejo": 10, "preco_atacado": 8,
                "estoque": 3, "nome_grupo": None, "similarity": 0.9, "tier": 0,
                "destaque": False, "estoque_grade": 3, "n_tamanhos": 1,
                "id_loja": "1", "loja": "L", "grupo_id": "g"}

    chamadas = []
    _real_rpc = bot._rpc_busca

    def _fake_rpc(params):
        chamadas.append(params.get('excluir_ids'))
        if params.get('excluir_ids'):
            return _Fake([_linha(0, 1)])                       # 1 < EXCLUIR_MIN_RESULTADOS
        return _Fake([_linha(i, 100 + i) for i in range(6)])

    bot._rpc_busca = _fake_rpc
    bot._set_turn_ctx("eval_ranking_fb", "tem calcinha?")
    bot._turn_ctx.excluir_ids[:] = ["9", "8", "7"]
    try:
        rfb = bot.consultar_estoque_supabase("calcinha", None, "244033")
    finally:
        bot._clear_turn_ctx()
        bot._rpc_busca = _real_rpc
    check("A11 refez a busca SEM exclusoes", len(chamadas) == 2 and chamadas[1] is None,
          f"chamadas={chamadas}")
    check("A11 devolveu o pool cheio em vez de vazio", len(rfb.get('produtos', [])) == 6,
          f"n={len(rfb.get('produtos', []))}")
    check("A11 declarou excluir_ids_aplicados=0",
          rfb.get('filtro_aplicado', {}).get('excluir_ids_aplicados') == 0,
          str(rfb.get('filtro_aplicado', {}).get('excluir_ids_aplicados')))

    print("\n=== A10: _ids_ja_mostrados nunca levanta (guard defensivo) ===")
    ok = True
    for entrada in (None, "", "user_inexistente", 12345, object()):
        try:
            bot._ids_ja_mostrados(entrada)
        except Exception as e:
            ok = False
            print(f"     levantou para {entrada!r}: {e}")
    check("A10 guard defensivo", ok)

    print("\n" + "=" * 70)
    if falhas:
        print(f"FALHOU: {len(falhas)} assert(s)")
        for f in falhas:
            print("  -", f)
        return 1
    print("TODOS OS ASSERTS PASSARAM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
