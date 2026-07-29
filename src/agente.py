"""Agente de base de conhecimento da Vitrinifarne.

Toda a lógica do agente, sem interface e sem notebook. Este é o módulo que roda
no servidor da OCI.

Fluxo: ler documentos -> quebrar em pedaços -> gerar embeddings -> montar índice
FAISS -> a cada pergunta, buscar os trechos relevantes e pedir ao Gemini uma
resposta baseada neles.

O índice é salvo em disco na primeira execução. Nas seguintes ele é carregado,
o que evita refazer as chamadas de embedding a cada reinício do servidor.

Uso:
    from agente import Agente
    agente = Agente()
    agente.preparar()
    print(agente.responder("qual o prazo para devolver um produto?"))
"""

import json
import os
import re
import unicodedata
from pathlib import Path

import faiss
import numpy as np
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from leitores import extrair

# ---------------------------------------------------------------- configuração

RAIZ = Path(__file__).resolve().parent.parent
PASTA_DOCS = RAIZ / "docs"
PASTA_INDICE = RAIZ / "indice"

MODELO_CHAT = os.environ.get("MODELO_CHAT", "gemini-flash-latest")
MODELO_EMBEDDING = os.environ.get("MODELO_EMBEDDING", "models/gemini-embedding-001")

TAMANHO_PEDACO = 900
SOBREPOSICAO = 150
TRECHOS_POR_PERGUNTA = 6
PESO_PALAVRAS = 0.20

INSTRUCOES = """Você é o assistente interno da Vitrinifarne, uma loja online de casa e decoração.
Sua função é responder perguntas de colaboradores e clientes usando exclusivamente a
documentação oficial da empresa.

Regras:
1. Responda apenas com base nos trechos fornecidos abaixo. Não invente políticas, prazos
   ou valores que não estejam nos trechos. Você pode usar conhecimento geral apenas para
   relacionar termos equivalentes — por exemplo, reconhecer que uma capital pertence a um
   estado citado nos trechos.
2. Sempre cite o documento e a versão de onde tirou a informação.
3. Se a resposta exigir combinar informações de documentos diferentes, faça a combinação
   e cite todas as fontes usadas.
4. Se a informação não estiver nos trechos, responda exatamente: "Não encontrei essa
   informação na documentação disponível." e sugira qual área procurar.
5. Responda em português do Brasil, de forma direta e objetiva. Sem saudações.
6. Valores em reais e prazos devem ser reproduzidos exatamente como aparecem nos trechos.

TRECHOS DA DOCUMENTAÇÃO:
{contexto}

PERGUNTA: {pergunta}

RESPOSTA:"""

PALAVRAS_IGNORADAS = {
    "para", "com", "uma", "quando", "qual", "quais", "onde", "como", "meu", "minha",
    "este", "esta", "esse", "essa", "dos", "das", "que", "nao", "sim", "por", "sobre",
    "pode", "posso", "tem", "quanto", "quantos", "quero", "preciso", "mais", "sua",
}


# ------------------------------------------------------------------- utilidades

def normalizar(texto):
    """Minúsculas e sem acento, para comparação de palavras."""
    texto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def texto_da_resposta(resposta):
    """Extrai o texto da resposta do modelo, que pode vir em blocos."""
    conteudo = resposta.content
    if isinstance(conteudo, str):
        return conteudo.strip()
    partes = []
    for bloco in conteudo:
        if isinstance(bloco, str):
            partes.append(bloco)
        elif isinstance(bloco, dict) and bloco.get("type") == "text":
            partes.append(bloco.get("text", ""))
    return "\n".join(p for p in partes if p).strip()


# ----------------------------------------------------------------------- agente

class Agente:
    """Base de conhecimento conversacional da Vitrinifarne."""

    def __init__(self, pasta_docs=PASTA_DOCS, pasta_indice=PASTA_INDICE):
        self.pasta_docs = Path(pasta_docs)
        self.pasta_indice = Path(pasta_indice)
        self.chunks = []
        self.chunks_normalizados = []
        self.indice = None
        self.embeddings = None
        self.modelo = None

    # -- preparação ---------------------------------------------------------

    def preparar(self, forcar_reindexacao=False):
        """Deixa o agente pronto para responder."""
        if os.environ.get("GOOGLE_API_KEY") is None:
            raise RuntimeError("Defina a variável de ambiente GOOGLE_API_KEY.")

        self.embeddings = GoogleGenerativeAIEmbeddings(model=MODELO_EMBEDDING)
        self.modelo = ChatGoogleGenerativeAI(model=MODELO_CHAT, temperature=0)

        arquivo_indice = self.pasta_indice / "indice.faiss"
        arquivo_chunks = self.pasta_indice / "chunks.json"

        if not forcar_reindexacao and arquivo_indice.exists() and arquivo_chunks.exists():
            self.indice = faiss.read_index(str(arquivo_indice))
            self.chunks = json.loads(arquivo_chunks.read_text(encoding="utf-8"))
            print(f"Índice carregado do disco: {self.indice.ntotal} pedaços.")
        else:
            self._indexar()
            self._salvar_indice()

        self.chunks_normalizados = [normalizar(c["texto"]) for c in self.chunks]
        return self

    def _ler_documentos(self):
        manifesto = json.loads(
            (self.pasta_docs / "manifesto.json").read_text(encoding="utf-8")
        )
        documentos = []
        for doc in manifesto["documentos"]:
            texto = extrair(self.pasta_docs / doc["arquivo"], doc["formato"])
            documentos.append({**doc, "texto": texto})
        return documentos

    def _indexar(self):
        documentos = self._ler_documentos()
        print(f"{len(documentos)} documentos lidos.")

        divisor = RecursiveCharacterTextSplitter(
            chunk_size=TAMANHO_PEDACO,
            chunk_overlap=SOBREPOSICAO,
            separators=["\n\n", "\n", ". ", "; ", " ", ""],
            length_function=len,
        )

        self.chunks = []
        for doc in documentos:
            for i, pedaco in enumerate(divisor.split_text(doc["texto"]), start=1):
                self.chunks.append({
                    "texto": f"[{doc['titulo']} — versão {doc['versao']}]\n{pedaco}",
                    "doc_id": doc["id"],
                    "titulo": doc["titulo"],
                    "arquivo": doc["arquivo"],
                    "versao": doc["versao"],
                    "categoria": doc["categoria"],
                    "parte": i,
                })
        print(f"{len(self.chunks)} pedaços gerados. Gerando embeddings...")

        vetores = self._gerar_embeddings([c["texto"] for c in self.chunks])
        faiss.normalize_L2(vetores)

        self.indice = faiss.IndexFlatIP(vetores.shape[1])
        self.indice.add(vetores)
        print(f"Índice criado: {self.indice.ntotal} vetores de {self.indice.d} dimensões.")

    def _gerar_embeddings(self, textos, tamanho_lote=10):
        """Gera os vetores em lotes, com plano B de um por vez."""
        vetores = []
        for i in range(0, len(textos), tamanho_lote):
            lote = textos[i:i + tamanho_lote]
            try:
                vetores.extend(self.embeddings.embed_documents(lote))
            except Exception:
                for texto in lote:
                    vetores.append(self.embeddings.embed_query(texto))
            print(f"  {min(i + tamanho_lote, len(textos))}/{len(textos)}")
        return np.array(vetores, dtype="float32")

    def _salvar_indice(self):
        self.pasta_indice.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.indice, str(self.pasta_indice / "indice.faiss"))
        (self.pasta_indice / "chunks.json").write_text(
            json.dumps(self.chunks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Índice salvo em {self.pasta_indice}")

    # -- busca -------------------------------------------------------------

    def _termos(self, pergunta):
        palavras = re.findall(r"\w+", normalizar(pergunta))
        return {p for p in palavras if len(p) > 3 and p not in PALAVRAS_IGNORADAS}

    def buscar(self, pergunta, quantidade=TRECHOS_POR_PERGUNTA):
        """Busca híbrida: significado (vetores) mais palavra (termos da pergunta)."""
        vetor = np.array([self.embeddings.embed_query(pergunta)], dtype="float32")
        faiss.normalize_L2(vetor)

        quantos = min(len(self.chunks), max(quantidade * 4, 20))
        notas, posicoes = self.indice.search(vetor, quantos)
        notas_vetor = {int(p): float(n) for n, p in zip(notas[0], posicoes[0])}

        termos = self._termos(pergunta)
        acertos = {
            i: sum(1 for t in termos if t in texto)
            for i, texto in enumerate(self.chunks_normalizados)
        }

        candidatos = set(notas_vetor) | {i for i, a in acertos.items() if a >= 2}

        resultados = []
        for posicao in candidatos:
            pedaco = dict(self.chunks[posicao])
            similaridade = notas_vetor.get(posicao, 0.0)
            pedaco["similaridade"] = similaridade
            pedaco["palavras"] = acertos[posicao]
            pedaco["nota_final"] = similaridade + PESO_PALAVRAS * (
                acertos[posicao] / max(len(termos), 1)
            )
            resultados.append(pedaco)

        resultados.sort(key=lambda r: r["nota_final"], reverse=True)
        return resultados[:quantidade]

    # -- resposta ----------------------------------------------------------

    def responder(self, pergunta, mostrar_fontes=True):
        """Responde à pergunta com base nos documentos da empresa."""
        if not pergunta or not pergunta.strip():
            return "Faça uma pergunta sobre a documentação da Vitrinifarne."

        achados = self.buscar(pergunta)
        contexto = "\n\n---\n\n".join(a["texto"] for a in achados)
        prompt = INSTRUCOES.format(contexto=contexto, pergunta=pergunta)

        try:
            resposta = texto_da_resposta(self.modelo.invoke(prompt))
        except Exception as erro:
            return f"Não foi possível consultar o modelo agora. Detalhe: {erro}"

        if mostrar_fontes:
            fontes = []
            for a in achados:
                marca = f"{a['titulo']} (v{a['versao']})"
                if marca not in fontes:
                    fontes.append(marca)
            resposta += "\n\n---\nTrechos consultados: " + "; ".join(fontes)

        return resposta


if __name__ == "__main__":
    agente = Agente().preparar()
    for pergunta in [
        "Qual o prazo para desistir de uma compra?",
        "Comprei uma estante modular para Belém. Quando chega e o frete é grátis?",
    ]:
        print("\n" + "=" * 70)
        print("PERGUNTA:", pergunta)
        print(agente.responder(pergunta))
