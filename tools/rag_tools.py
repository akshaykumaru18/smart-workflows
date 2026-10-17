"""Load a CSV, PDF, or text file, chunk it, and answer from those chunks only.

Set GEMINI_API_KEY before calling build_retriever or answer_question.

Chunks are embedded on this machine with fastembed. The model and its
cache folder are set in this file. Chat answers still use Gemini.
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
EMBEDDING_PROVIDER = "local"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_CACHE = Path.home() / ".fastembed"
EMBEDDING_BATCH_SIZE = 5
EMBEDDING_BATCH_PAUSE = 2.0
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
CSV_ROWS_PER_CHUNK = 30
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
            "Use only the context. When the user asks you to explain, summarize, "
            "or simplify a part of the document, rewrite that part from the context "
            "in the style they asked for. "
            "If the context does not contain the part they asked about, "
            f"reply exactly: {NOT_FOUND} "
            "Do not add facts that are not in the context.",
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
    """Split documents into chunks and number each chunk.

    A CSV is grouped into chunks of 30 rows. Other files are split by
    character size with overlap.
    """
    if _is_csv(documents):
        chunks = _group_csv_rows(documents, CSV_ROWS_PER_CHUNK)
    else:
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


def retrieve(retriever, question: str, chunks: list[Document] | None = None) -> list[Document]:
    """Return the chunks most similar to the question.

    A question that names a section, such as the abstract, uses the chunks
    that contain that heading. Similarity search misses those when the
    wording of the question is unlike the section text.
    """
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    section_hits = _section_chunks(chunks or [], question)
    if section_hits:
        return section_hits
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
        return retrieve(self.retriever, question, self.chunks)

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


def _is_csv(documents: list[Document]) -> bool:
    if not documents:
        return False
    source = str(documents[0].metadata.get("source") or "")
    return Path(source).suffix.lower() == ".csv"


def _group_csv_rows(documents: list[Document], rows_per_chunk: int) -> list[Document]:
    """Join CSV row documents into chunks of rows_per_chunk rows."""
    chunks = []
    for start in range(0, len(documents), rows_per_chunk):
        group = documents[start:start + rows_per_chunk]
        content = "\n\n".join(
            document.page_content.strip()
            for document in group
            if document.page_content.strip()
        )
        if not content:
            continue
        metadata = dict(group[0].metadata)
        metadata["row_start"] = group[0].metadata.get("row", start)
        metadata["row_end"] = group[-1].metadata.get("row", start + len(group) - 1)
        chunks.append(Document(page_content=content, metadata=metadata))
    return chunks


def _vector_store(chunks: list[Document]) -> InMemoryVectorStore:
    return InMemoryVectorStore.from_documents(chunks, _embeddings())


def _embeddings():
    provider = EMBEDDING_PROVIDER
    if provider == "local":
        return LocalEmbeddings(EMBEDDING_MODEL, EMBEDDING_CACHE)
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
    """Embeddings from a fastembed model stored in a short cache folder."""

    def __init__(self, model_name: str, cache_dir: Path):
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise RuntimeError(
                "Local embeddings need fastembed. Install it with: pip install fastembed"
            ) from error
        print(f"Loading local embedding model {model_name}", flush=True)
        self.model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(self.model.query_embed(text)).tolist()


def _batch_size() -> int:
    return EMBEDDING_BATCH_SIZE


def _batch_pause() -> float:
    return EMBEDDING_BATCH_PAUSE


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


def _named_section(question: str) -> str | None:
    for name in ("abstract", "introduction", "conclusion", "references"):
        if re.search(rf"\b{name}\b", question, flags=re.IGNORECASE):
            return name
    return None


def _section_chunks(chunks: list[Document], question: str) -> list[Document]:
    section = _named_section(question)
    if section is None:
        return []
    heading = re.compile(rf"(?im)(?:^|\n)\s*{section}\s*[—–:\-.]")
    selected = []
    seen = set()
    for index, chunk in enumerate(chunks):
        if not heading.search(chunk.page_content):
            continue
        for follow in chunks[index:index + 3]:
            key = follow.metadata.get("chunk")
            if key in seen:
                continue
            seen.add(key)
            selected.append(follow)
    return selected


def _location(document: Document) -> str:
    source = document.metadata.get("source") or ""
    page = document.metadata.get("page")
    if page is None:
        return str(source)
    return f"{source} page {int(page) + 1}"
