# F3 — bloco de FOTOS para o deploy único de prompt (F6)

**STAGED. NÃO deployar isolado.** O prompt vive em `bot_settings.system_prompt` (id=1) e
sai por `inserir_prompt.py`; um deploy só para este bloco gastaria a janela do deploy
único da F6 e invalidaria o baseline da eval sem necessidade.

Linhas referenciadas = prompt vivo no banco em **2026-08-09** (366 linhas). Confira antes
de aplicar: `select system_prompt from bot_settings where id=1;`.

## Pré-requisitos operacionais (checar ANTES de deployar)

| # | Requisito | Se falhar |
|---|---|---|
| 1 | `FOTOS_SOB_DEMANDA=1` no Railway (default do código já é 1; só falha se alguém setou 0) | Com o switch em 0 a tool **não existe** na function declaration e o prompt mandaria chamar ferramenta inexistente |
| 2 | `replicas = 1` no serviço web (Gate 0.1) | `_fotos_vistas` é memória do processo: com 2 réplicas a Luna diz "são todas que eu tenho" sendo mentira, e a mesma foto pode sair duas vezes |
| 3 | Teto de fotos: `FOTOS_MAX_POR_PEDIDO` (default 4). Decisão comercial §10.3 item 7 ainda aberta | Nada quebra — é env var, sem redeploy de código |

## 1. §0 REGRAS DO JSON — **linha 14**, acrescentar ao fim da linha

> Cada produto traz também `n_fotos`: quantas fotos existem daquele produto no total. Se
> `n_fotos` for maior que 1, existem OUTROS ÂNGULOS além da foto do card — e você pode
> mostrá-los chamando `mostrar_fotos_produto` (ver §5). Você NUNCA escreve URL de imagem
> em `resposta_texto`: quem envia imagem é sempre o sistema.

## 2. §2 FLUXO — **linha 37**, acrescentar ao fim da linha

> Se dois produtos empatarem, prefira o de `n_fotos` maior: o cliente consegue ver mais
> ângulos da peça.

## 3. §5 — substituir a **linha 75** inteira (o parágrafo `FOTOS:`)

> FOTOS: o card só tem foto quando o produto vem com `tem_foto: true`. Se o cliente PEDIR
> fotos e NENHUM produto da categoria certa tiver foto, NÃO prometa imagem — seja
> transparente e continue vendendo pelo texto. Ex.: "Esse modelo ainda não tem foto
> cadastrada aqui no sistema, mas te passo nome, preço e todos os detalhes 💕". NUNCA diga
> "vou te mandar as fotos" / "dá uma olhadinha" quando não há foto a enviar.
>
> MAIS FOTOS (outros ângulos): quando o cliente pedir mais imagens de uma peça específica
> ("manda mais fotos", "tem outra foto?", "quero ver melhor", "de outro ângulo"), chame
> `mostrar_fotos_produto(id_produto)`. O sistema envia as fotos automaticamente DEPOIS da
> sua resposta — você só anuncia. Um produto por resposta.
>
> Ao anunciar, obedeça:
> - Fale de forma NEUTRA: "já te mando outros ângulos que eu tenho dessa peça".
> - NUNCA diga qual lado/ângulo a foto mostra ("aqui a de trás", "essa é a frente"). O
>   sistema não sabe o que cada foto mostra.
> - NUNCA afirme a COR de uma peça a partir de foto: fotos do mesmo produto podem ser de
>   outra cor ou de outra estampa. Cor quem confirma é a atendente (§7).
> - Anuncie no máximo o número que veio em `vai_enviar`.
> - Se `restantes` for maior que 0 e o cliente pedir mais, chame a ferramenta de novo no
>   turno seguinte.
> - Se o status for `sem_novas`, o cliente já recebeu TODAS as fotos: diga que são todas
>   que você tem e ofereça uma atendente (motivo `pedido_foto`, §15).
> - Se o status for `sem_foto`, seja transparente e NÃO ofereça atendente por causa da foto.
> - Nunca diga que a atendente vai TIRAR ou MANDAR uma foto nova.

## 4. §15 TRANSFERÊNCIA — três edições

**Linha 285:** `Chame transferir_para_atendente em 4 casos.` → **`em 5 casos.`**

**Linha 292 (depois da linha do caso 4):** inserir a nova linha da tabela

> `| 5 | pedido_foto | Cliente já recebeu TODAS as fotos que existem (status sem_novas) e ainda quer ver mais da peça | ` **[BLOQUEADO — §10.2 item 2]** `|`

**Mensagem-padrão do caso 5 — depende do dono da loja.** A pergunta aberta é se a
atendente vai *tirar foto nova* da peça ou apenas descrever/mandar vídeo. Até a resposta,
use a redação que **não promete foto**:

> "Essas são todas as fotos que eu tenho dessa peça aqui no sistema 💕 Vou chamar uma
> atendente para te dar mais detalhes dela, um momento 😊"

Se o dono confirmar que a atendente tira foto, trocar por: *"...vou chamar uma atendente
para te mostrar mais dessa peça"*. **Não deployar a versão que promete foto sem essa
confirmação** — é o que reprovou `photo_transparency` no cenário de fantasia sem foto.

**Linha 295:** `- motivo: um dos 4 acima.` → **`um dos 5 acima.`**

**Linha 303:** `- NUNCA transfira em casos fora desses 4.` → **`fora desses 5.`**

Sem DDL: não há constraint em `conversation_handoffs.motivo`, então o motivo novo entra
sem migration.

## 5. O que este bloco deliberadamente NÃO faz

- **Não** manda a Luna oferecer fotos por conta própria. O gatilho é o cliente pedir; a
  tool anuncia, não vende.
- **Não** menciona a foto de trás/frente nem cor em nenhuma redação — não existe dado de
  ordem, tipo ou cor em `produtos_imagens` (decisão do dono: "outros ângulos que eu tenho").
- **Não** promete número fixo de fotos: o teto é env (`FOTOS_MAX_POR_PEDIDO`), então
  qualquer número escrito no prompt viraria mentira quando o dono mudar o teto. O modelo
  usa `vai_enviar`.

## 6. Gate depois do deploy (F6)

1. `run_eval.py` completo e **novo baseline** (o prompt está no banco: todo baseline
   anterior vira histórico).
2. Conferir no log: `[fotos] tool_chamada=1 extras_enviadas=N` — mede a taxa de
   acionamento. Sem o bloco de prompt a tool já era acionada só pela docstring
   (`--dimension foto` verde), então a expectativa é que **suba**, não que apareça.
3. `select wamid, id_produto, legenda, created_at from card_envios order by created_at
   desc limit 10;` → 1 row por card + 1 por foto extra, `id_produto` correto.
4. Dump do prompt do banco de volta em `prompt_luna_v2.txt` (a cópia local está defasada).
