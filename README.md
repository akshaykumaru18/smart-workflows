# LangChain document RAG

Ask questions about one CSV, PDF, or text file. The file is split into chunks, embedded, and stored in memory. Each answer is written from the chunks most similar to the question.

## Retrieval strategy

The retriever is **dense top-k similarity search**.

1. Every chunk is embedded with `gemini-embedding-001` using the `RETRIEVAL_DOCUMENT` task.
2. The question is embedded with the same model using the `RETRIEVAL_QUERY` task.
3. Chunks live in LangChain's `InMemoryVectorStore`, which ranks them by **cosine similarity**.
4. `as_retriever(search_kwargs={"k": 4})` uses LangChain's default `search_type="similarity"` and returns the **4 closest chunks**.
5. If the question names the abstract, introduction, conclusion, or references, those heading chunks are used instead of the similarity result. A request to explain that section in simpler words is answered from those chunks.

Those chunks are placed into one prompt. The chat model answers from that text only.

| Setting | Value |
| --- | --- |
| Search | similarity, top 4 |
| Score | cosine similarity |
| Embeddings | `gemini-embedding-001`, batches of 5 with a 2 second pause |
| Vector store | in memory, rebuilt each time a file is indexed |
| Chunk size | 800 characters |
| Chunk overlap | 120 characters |
| Chat model | `gemini-flash-lite-latest`, temperature 0 |

`k`, chunk size, and chunk overlap are arguments of `index_file` in `tools/rag_tools.py`. `RAG_CHAT_MODEL` and `RAG_EMBEDDING_MODEL` override the model names. `RAG_EMBEDDING_BATCH_SIZE` and `RAG_EMBEDDING_BATCH_PAUSE` slow Google embedding calls down. `RAG_EMBEDDING_PROVIDER=local` uses `all-MiniLM-L6-v2` on this machine instead of the Google quota (`pip install sentence-transformers`).

## Run it

Practice the model, prompt, and chain first:

```bash
export GEMINI_API_KEY="your-gemini-key"
python3 langchain_basics.py
```

Then index a file and ask questions:

```bash
python3 rag_workflow.py discounts.csv
```

With no path, the program asks for one. At the `Question:` prompt:

- type a question about the indexed file
- `file <path>` indexes a different file
- `exit` stops

`GOOGLE_API_KEY` is accepted when `GEMINI_API_KEY` is unset. A `.env` file in this directory is loaded when `python-dotenv` is installed.

## What happens to a file

`index_file` in `tools/rag_tools.py` runs three steps.

**Load.** The extension picks the loader.

| Extension | Loader | Documents |
| --- | --- | --- |
| `.csv` | `CSVLoader` | one document per row, column names kept |
| `.pdf` | `PyPDFLoader` | one document per page |
| `.txt`, `.text`, `.md` | `TextLoader` | the whole file |

Empty documents are dropped. Any other extension raises an error.

**Chunk.** `RecursiveCharacterTextSplitter` splits on paragraphs, lines, spaces, then characters, keeping 120 characters of overlap. Each chunk is numbered in `metadata["chunk"]`. A short CSV row stays one chunk. A long PDF page becomes several.

**Retrieve and answer.** `build_retriever` embeds the chunks and returns the similarity retriever. `IndexedFile.answer` retrieves the top 4 chunks, sends them to Gemini, and returns the answer plus those chunks as sources. The workflow prints the answer and a short preview of each source.

If retrieval returns nothing, or the model finds no answer in the context, the reply is exactly:

```text
I cannot find that in the document.
```

The system prompt tells the model to stay inside the retrieved context and to avoid guessing.

## Files

| File | Role |
| --- | --- |
| `langchain_basics.py` | Practice file: one model call, a prompt template, then `prompt \| model \| StrOutputParser()` |
| `rag_workflow.py` | Command-line loop. Asks for a path, indexes it, and prints answers with sources |
| `tools/rag_tools.py` | `load_documents`, `chunk_documents`, `build_retriever`, `retrieve`, `answer_question`, and `index_file` |

Call the tools directly when you want to inspect each step:

```python
from tools.rag_tools import load_documents, chunk_documents, build_retriever, answer_question

documents = load_documents("discounts.csv")
chunks = chunk_documents(documents)
retriever = build_retriever(chunks, k=4)
print(answer_question("What discount did order 2 get?", retriever))
```

## Limits

The index is held in memory for the current process. Starting the program again embeds the file again.

A question only sees the 4 closest chunks. A total, average, or other result that depends on every row needs those rows to be among the retrieved chunks. A lookup such as one order id or one product name matches this retriever directly.
