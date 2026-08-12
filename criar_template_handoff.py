"""Cria (ou audita) o template `handoff_atendente` na WhatsApp Cloud API.

POR QUE EXISTE: a atendente humana NUNCA escreve para o número do bot, então ela está
sempre fora da janela de 24h da Cloud API. Texto livre para fora da janela **não é
entregue** — medido em 12/08/2026: a Meta respondeu HTTP 200 e a mensagem nunca chegou.
Template aprovado é o único envio proativo que atravessa a janela.

CREDENCIAIS — nenhuma fica no repo. Exporte antes de rodar:

    export WHATSAPP_WABA_ID=...      # WhatsApp Business Account ID (NÃO é o phone_number_id)
    export WHATSAPP_TOKEN=...        # token com permissão whatsapp_business_management

Onde achar o WABA ID: Meta Business Suite > Configurações do WhatsApp > Contas do
WhatsApp Business > (a conta) > "ID da conta do WhatsApp Business".
O `WHATSAPP_PHONE_NUMBER_ID` que o bot já usa NÃO serve aqui — é outro objeto.

USO:
    python criar_template_handoff.py --check     # lista os templates e o status de aprovação
    python criar_template_handoff.py            # cria o handoff_atendente
    python criar_template_handoff.py --dry-run  # mostra o payload sem enviar

Depois de criado, a Meta analisa (utility costuma sair em minutos a poucas horas).
O bot só passa a usar quando o status virar APPROVED — antes disso o
`enviar_alerta_operador` cai no texto livre automaticamente.
"""
import json
import os
import sys

import requests

GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
WABA_ID = os.getenv("WHATSAPP_WABA_ID")
TOKEN = os.getenv("WHATSAPP_TOKEN")

NOME = os.getenv("HANDOFF_TEMPLATE", "handoff_atendente")
LANG = os.getenv("HANDOFF_TEMPLATE_LANG", "pt_BR")

# Restrições da Meta que este corpo respeita, e que são motivo comum de rejeição:
#  - não começa nem termina com variável;
#  - nunca duas variáveis adjacentes;
#  - numeração sequencial 1..4, sem pular;
#  - abaixo de 1024 caracteres depois da substituição.
# O histórico da conversa NÃO entra: parâmetro de template não aceita quebra de linha, e
# o corpo tem teto de 1024. A atendente recebe o número do cliente e abre a conversa.
CORPO = (
    "Novo atendimento da Luna 🔔\n"
    "\n"
    "Cliente: {{1}}\n"
    "Motivo: {{2}}\n"
    "\n"
    "Resumo: {{3}}\n"
    "\n"
    "Produtos citados: {{4}}\n"
    "\n"
    "Abra a conversa com o cliente pelo número acima para continuar o atendimento."
)

EXEMPLO = [[
    "5527999465394",
    "Cliente pediu para falar com uma pessoa",
    "Cliente quer confirmar se a Camisola Helena tem em rosa no tamanho M.",
    "Camisola Helena (cod. 8147660)",
]]

PAYLOAD = {
    "name": NOME,
    "language": LANG,
    "category": "UTILITY",          # utility = notificação transacional; NÃO é marketing
    "components": [{
        "type": "BODY",
        "text": CORPO,
        "example": {"body_text": EXEMPLO},
    }],
}


def _exigir_credenciais():
    faltando = [n for n, v in (("WHATSAPP_WABA_ID", WABA_ID), ("WHATSAPP_TOKEN", TOKEN)) if not v]
    if faltando:
        print(f"[erro] variáveis ausentes: {', '.join(faltando)}")
        print("       veja o cabeçalho deste arquivo para onde achar cada uma.")
        sys.exit(2)


def checar():
    _exigir_credenciais()
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WABA_ID}/message_templates"
    r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"},
                     params={"limit": 50}, timeout=30)
    if not r.ok:
        err = (r.json().get("error") or {}) if r.text else {}
        print(f"[erro] {r.status_code}: code={err.get('code')} msg={err.get('message')}")
        sys.exit(1)
    dados = r.json().get("data") or []
    print(f"{len(dados)} template(s) na conta:\n")
    for t in dados:
        marca = " <== o do handoff" if t.get("name") == NOME else ""
        print(f"  {t.get('status'):<10} {t.get('category'):<12} {t.get('language'):<6} "
              f"{t.get('name')}{marca}")
    if not any(t.get("name") == NOME for t in dados):
        print(f"\n'{NOME}' AINDA NÃO EXISTE — rode sem --check para criar.")
    else:
        print(f"\nO bot só usa '{NOME}' quando o status for APPROVED.")


def criar():
    _exigir_credenciais()
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WABA_ID}/message_templates"
    r = requests.post(url, headers={"Authorization": f"Bearer {TOKEN}",
                                    "Content-Type": "application/json"},
                      json=PAYLOAD, timeout=30)
    if not r.ok:
        err = (r.json().get("error") or {}) if r.text else {}
        print(f"[erro] {r.status_code}: code={err.get('code')} msg={err.get('message')}")
        print("       132000/132001 = corpo inválido ou nome já existe;")
        print("       190 = token sem permissão whatsapp_business_management.")
        sys.exit(1)
    resp = r.json()
    print(f"[ok] template criado: id={resp.get('id')} status={resp.get('status')} "
          f"categoria={resp.get('category')}")
    print("     APPROVED = já vale. PENDING = a Meta ainda está analisando.")
    print(f"     confira depois com: python {os.path.basename(__file__)} --check")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        print(f"POST /{GRAPH_VERSION}/<WABA_ID>/message_templates")
        print(json.dumps(PAYLOAD, ensure_ascii=False, indent=2))
    elif "--check" in sys.argv:
        checar()
    else:
        criar()
