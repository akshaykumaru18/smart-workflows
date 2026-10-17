"""Load a CSV, PDF, or text file, chunk it, and answer from those chunks only.

Set GEMINI_API_KEY before calling build_retriever or answer_question.

Google embeddings are sent in small batches and retried after a rate limit.
If the quota is exhausted, embed on this machine instead:

    export RAG_EMBEDDING_PROVIDER=local
    pip install sentence-transformers
"""

import os
import re
import time
from pathlib import Path

from langchain_community.document_loaders import CSVLoader, PyPDFLoader, TextLoader
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

CHAT_MODEL = os.environ.get("RAG_CHAT_MODEL", "gemini-flash-lite-latest")
EMBEDDING_PROVIDER = os.environ.get("RAG_EMBEDDING_PROVIDER", "google").strip().lower()
EMBEDDING_MODEL = os.environ.get("RAG_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_BATCH_SIZE = 5
EMBEDDING_BATCH_PAUSE = 2.0
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
RETRIEVAL_K = 4
NOT_FOUND = "I cannot find that in the document."

LOADERS = {
    ".csv": CSVLoader,
    ".pdf": PyPDFLoader,
    ".txt": TextLoader,
    ".text": TextLoader,
    ".md": TextLoader,
}

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You answer questions about one uploaded document. "
            "Use only the context. If the context does not contain the answer, "
            f"reply exactly: {NOT_FOUND} "
            "Do not use outside knowledge, and do not guess.",
        ),
        ("human", "Context:\n{context}\n\nQuestion: {question}"),
    ]
)


def load_documents(file_path: str) -> list[Document]:
    """Load a CSV, PDF, or text file. CSV becomes one document per row."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(file_path)

    suffix = path.suffix.lower()
    loader_type = LOADERS.get(suffix)
    if loader_type is None:
        supported = ", ".join(sorted(LOADERS))
        raise ValueError(f"Unsupported file type '{suffix}'. Use one of: {supported}")

    if suffix == ".pdf":
        documents = PyPDFLoader(str(path)).load()
    else:
        documents = loader_type(str(path), encoding="utf-8", autodetect_encoding=True).load()

    documents = [document for document in documents if document.page_content.strip()]
    if not documents:
        raise ValueError(f"No text found in {path}")
    return documents


def chunk_documents(
    documents: list[Document],
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[Document]:
    """Split documents into overlapping chunks and number each chunk."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    chunks = splitter.split_documents(documents)
    chunks = [chunk for chunk in chunks if chunk.page_content.strip()]
    if not chunks:
        raise ValueError("Chunking produced no text")

    for index, chunk in enumerate(chunks, start=1):
        chunk.metadata["chunk"] = index
    return chunks


def build_retriever(chunks: list[Document], k: int = RETRIEVAL_K):
    """Embed chunks and return a similarity retriever."""
    if not chunks:
        raise ValueError("chunks is empty")
    if k <= 0:
        raise ValueError("k must be greater than 0")

    store = _vector_store(chunks)
    return store.as_retriever(search_kwargs={"k": k})


def retrieve(retriever, question: str) -> list[Document]:
    """Return the chunks most similar to the question."""
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    return retriever.invoke(question)


def answer_question(question: str, retriever) -> str:
    """Answer using only chunks returned by the retriever."""
    documents = retrieve(retriever, question)
    if not documents:
        return NOT_FOUND
    return _generate_answer(question, documents)


class IndexedFile:
    """A file that has been loaded, chunked, and indexed for retrieval."""

    def __init__(
        self,
        path: str,
        documents: list[Document],
        chunks: list[Document],
        store: InMemoryVectorStore,
        retriever,
    ):
        self.path = path
        self.documents = documents
        self.chunks = chunks
        self.store = store
        self.retriever = retriever

    def print_stored_embeddings(self) -> None:
        """Print every embedding currently held in the in-memory vector store."""
        entries = sorted(
            self.store.store.values(),
            key=lambda item: item["metadata"].get("chunk") or 0,
        )
        print(f"Embeddings in memory: {len(entries)}")
        for item in entries:
            chunk = item["metadata"].get("chunk")
            vector = [float(value) for value in item["vector"]]
            print(f"[{chunk}] {vector}")

    def retrieve(self, question: str) -> list[Document]:
        """Return the chunks most similar to the question."""
        return retrieve(self.retriever, question)

    def answer(self, question: str) -> dict:
        """Answer from retrieved chunks and include those chunks as sources."""
        documents = self.retrieve(question)
        if not documents:
            return {"answer": NOT_FOUND, "sources": []}
        return {
            "answer": _generate_answer(question, documents),
            "sources": [_source(document) for document in documents],
        }


def index_file(
    file_path: str,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    k: int = RETRIEVAL_K,
) -> IndexedFile:
    """Load a file, chunk it, and build a retriever."""
    if k <= 0:
        raise ValueError("k must be greater than 0")
    documents = load_documents(file_path)
    chunks = chunk_documents(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    store = _vector_store(chunks)
    retriever = store.as_retriever(search_kwargs={"k": k})
    return IndexedFile(str(Path(file_path).expanduser()), documents, chunks, store, retriever)


def _vector_store(chunks: list[Document]) -> InMemoryVectorStore:
    return InMemoryVectorStore.from_documents(chunks, _embeddings())


def _embeddings():
    provider = EMBEDDING_PROVIDER
    if provider == "local":
        return LocalEmbeddings(_local_model_name())
    if provider != "google":
        raise ValueError("RAG_EMBEDDING_PROVIDER must be google or local")
    # Leave task_type unset so documents use RETRIEVAL_DOCUMENT and
    # questions use RETRIEVAL_QUERY.
    return PacedGoogleEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=_api_key(),
    )


class PacedGoogleEmbeddings(GoogleGenerativeAIEmbeddings):
    """Google embeddings with small batches, a pause, and rate-limit retries."""

    def embed_documents(self, texts: list[str], batch_size: int | None = None, **kwargs):
        size = batch_size or _batch_size()

        def embed_batch(batch: list[str]) -> list[list[float]]:
            return super(PacedGoogleEmbeddings, self).embed_documents(
                batch,
                batch_size=max(len(batch), 1),
                **kwargs,
            )

        return _embed_in_batches(embed_batch, list(texts), size)

    def embed_query(self, text: str, **kwargs) -> list[float]:
        return _with_retry(lambda: super().embed_query(text, **kwargs))


class LocalEmbeddings:
    """Embeddings from a sentence-transformers model on this machine."""

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "Local embeddings need sentence-transformers. "
                "Install it with: pip install sentence-transformers"
            ) from error
        print(f"Loading local embedding model {model_name}", flush=True)
        self.model = SentenceTransformer(model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _local_model_name() -> str:
    configured = os.environ.get("RAG_EMBEDDING_MODEL", "").strip()
    if not configured or configured.startswith("gemini-") or configured.startswith("models/"):
        return "all-MiniLM-L6-v2"
    return configured


def _batch_size() -> int:
    raw = os.environ.get("RAG_EMBEDDING_BATCH_SIZE", str(EMBEDDING_BATCH_SIZE))
    try:
        size = int(raw)
    except ValueError as error:
        raise ValueError("RAG_EMBEDDING_BATCH_SIZE must be a positive integer") from error
    if size <= 0:
        raise ValueError("RAG_EMBEDDING_BATCH_SIZE must be a positive integer")
    return size


def _batch_pause() -> float:
    raw = os.environ.get("RAG_EMBEDDING_BATCH_PAUSE", str(EMBEDDING_BATCH_PAUSE))
    try:
        pause = float(raw)
    except ValueError as error:
        raise ValueError("RAG_EMBEDDING_BATCH_PAUSE must be a number of seconds") from error
    if pause < 0:
        raise ValueError("RAG_EMBEDDING_BATCH_PAUSE must be >= 0")
    return pause


def _embed_in_batches(embed_batch, texts: list[str], size: int) -> list[list[float]]:
    vectors = []
    total = len(texts)
    pause = _batch_pause()
    for start in range(0, total, size):
        batch = texts[start:start + size]
        if total > size:
            print(
                f"Embedding {start + 1}-{start + len(batch)} of {total}",
                flush=True,
            )
        vectors.extend(_with_retry(lambda batch=batch: embed_batch(batch)))
        if pause > 0 and start + size < total:
            time.sleep(pause)
    return vectors


def _with_retry(call):
    attempts = 5
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:
            if not _is_rate_limit(error) or attempt == attempts - 1:
                if _is_rate_limit(error):
                    raise RuntimeError(
                        f"{error} Google embedding quota is still exhausted. "
                        "Wait and retry, or set RAG_EMBEDDING_PROVIDER=local."
                    ) from error
                raise
            delay = _retry_seconds(error, attempt)
            print(f"Embedding rate limit. Waiting {delay:.0f}s.", flush=True)
            time.sleep(delay)


def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "resource exhausted" in text
        or "rate limit" in text
        or "quota" in text
    )


def _retry_seconds(error: Exception, attempt: int) -> float:
    match = re.search(r"retry in ([0-9.]+)\s*s", str(error), flags=re.IGNORECASE)
    if match:
        return float(match.group(1)) + 0.5
    return min(60.0, float(2 ** (attempt + 1)))


def _chat_model() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=CHAT_MODEL,
        google_api_key=_api_key(),
        temperature=0,
    )


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY before running.")
    return key


def _generate_answer(question: str, documents: list[Document]) -> str:
    context = "\n\n".join(_source_text(document) for document in documents)
    chain = ANSWER_PROMPT | _chat_model() | StrOutputParser()
    answer = chain.invoke({"context": context, "question": question.strip()}).strip()
    return answer or NOT_FOUND


def _source_text(document: Document) -> str:
    chunk = document.metadata.get("chunk", "?")
    location = _location(document)
    return f"[{chunk}] {location}\n{document.page_content.strip()}"


def _source(document: Document) -> dict:
    return {
        "chunk": document.metadata.get("chunk"),
        "location": _location(document),
        "text": document.page_content.strip(),
    }


def _location(document: Document) -> str:
    source = document.metadata.get("source") or ""
    page = document.metadata.get("page")
    if page is None:
        return str(source)
    return f"{source} page {int(page) + 1}"
