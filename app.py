"""Interface web do agente da Vitrinifarne.

Sobe um servidor com uma tela de chat. É este arquivo que roda na OCI.

Execução local:
    export GOOGLE_API_KEY="sua-chave"
    python app.py

Depois acesse http://localhost:7860
"""

import os
import sys
from pathlib import Path

import gradio as gr

sys.path.append(str(Path(__file__).resolve().parent / "src"))

from agente import Agente

PORTA = int(os.environ.get("PORTA", 7860))

EXEMPLOS = [
    "Qual o prazo para desistir de uma compra?",
    "Em quantas vezes posso parcelar?",
    "Quanto tempo demora para o dinheiro voltar?",
    "Um cliente quer devolver um produto de higiene pessoal com o lacre aberto. Pode?",
    "Comprei uma estante modular para Belém. Quando chega e o frete é grátis?",
    "Quais estados ficam na região Sul para efeito de frete?",
]

DESCRICAO = """Assistente interno da **Vitrinifarne**, loja online de casa e decoração.

Responde com base em 8 documentos oficiais da empresa — política de privacidade,
termos e condições, FAQ, guia de envios, tabela de prazos e fretes, catálogo de
produtos, política de reembolso e material de treinamento do atendimento.

Toda resposta cita o documento e a versão consultados. Quando a informação não
está na documentação, o assistente informa isso em vez de improvisar."""

print("Preparando o agente...")
agente = Agente().preparar()
print("Agente pronto.")


def conversar(mensagem, historico):
    """O Gradio chama esta função a cada pergunta enviada."""
    return agente.responder(mensagem)


interface = gr.ChatInterface(
    fn=conversar,
    title="Agente Vitrinifarne",
    description=DESCRICAO,
    examples=EXEMPLOS,
    theme=gr.themes.Soft(),
)

if __name__ == "__main__":
    interface.launch(server_name="0.0.0.0", server_port=PORTA)
