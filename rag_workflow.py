"""Ask questions about one CSV, PDF, or text file.

The file is chunked and indexed. Answers come only from retrieved chunks.

    export GEMINI_API_KEY="your-gemini-key"
    python3 rag_workflow.py
    python3 rag_workflow.py discounts.csv

During the question loop, `file <path>` indexes a different file.
"""

import sys

from tools.rag_tools import index_file


def run() -> None:
    file_path = sys.argv[1] if len(sys.argv) > 1 else input("File path: ").strip()
    if not file_path:
        print("A file path is required.")
        return

    indexed = _index(file_path)
    if indexed is None:
        return

    while True:
        question = input("Question: ").strip()
        if question.lower() in {"exit", "quit"}:
            break
        if not question:
            continue
        if question.lower().startswith("file "):
            indexed = _index(question[5:].strip()) or indexed
            continue

        try:
            result = indexed.answer(question)
        except Exception as error:
            print(f"Could not answer: {error}")
            continue

        print(result["answer"])
        if result["sources"]:
            print("Sources:")
            for source in result["sources"]:
                preview = source["text"].replace("\n", " ")
                if len(preview) > 240:
                    preview = preview[:240] + "..."
                print(f"  [{source['chunk']}] {source['location']}: {preview}")


def _index(file_path: str):
    try:
        indexed = index_file(file_path)
    except Exception as error:
        print(f"Could not index {file_path}: {error}")
        return None

    print(
        f"Indexed {indexed.path}: {len(indexed.documents)} documents, "
        f"{len(indexed.chunks)} chunks."
    )
    indexed.print_stored_embeddings()
    return indexed


if __name__ == "__main__":
    run()
