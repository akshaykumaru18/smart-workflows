"""Practice LangChain before the RAG workflow.

Three pieces show up again in rag_workflow.py:
a chat model, a prompt template, and a chain that connects them.

    export GEMINI_API_KEY="your-gemini-key"
    python3 langchain_basics.py

Change PRACTICE_TASK, then type your own topic at the prompt.
"""

import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

MODEL = os.environ.get("RAG_CHAT_MODEL", "gemini-flash-lite-latest")

# Edit this sentence, then run the file again.
PRACTICE_TASK = "Explain the topic in two short sentences for a beginner."


def chat_model() -> ChatGoogleGenerativeAI:
    """Create the same style of Gemini chat model the RAG workflow uses."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY before running.")
    return ChatGoogleGenerativeAI(model=MODEL, google_api_key=key, temperature=0)


def lesson_model_call(model: ChatGoogleGenerativeAI) -> None:
    """Send one system message and one human message. The result is an AI message."""
    print("\n1. Model call")
    response = model.invoke(
        [
            SystemMessage(content="Answer in one sentence."),
            HumanMessage(content="What is a prompt template?"),
        ]
    )
    print(response.content)


def lesson_prompt_template() -> None:
    """Fill {topic} and {task} before any model sees the prompt."""
    print("\n2. Prompt template")
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You teach LangChain. Keep the answer short."),
            ("human", "Topic: {topic}\nTask: {task}"),
        ]
    )
    messages = prompt.format_messages(
        topic="a chain",
        task="Say what the | symbol does.",
    )
    for message in messages:
        print(f"{message.type}: {message.content}")


def lesson_chain(model: ChatGoogleGenerativeAI) -> None:
    """Connect prompt, model, and parser. The chain returns a plain string."""
    print("\n3. Chain")
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", PRACTICE_TASK),
            ("human", "{topic}"),
        ]
    )
    chain = prompt | model | StrOutputParser()
    answer = chain.invoke({"topic": "an output parser"})
    print(answer)


def practice(model: ChatGoogleGenerativeAI) -> None:
    """Ask your own topics through the same chain shape."""
    print("\n4. Your turn")
    print("Type a topic. The chain uses PRACTICE_TASK at the top of this file.")
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", PRACTICE_TASK),
            ("human", "{topic}"),
        ]
    )
    chain = prompt | model | StrOutputParser()

    while True:
        topic = input("Topic: ").strip()
        if topic.lower() in {"exit", "quit"}:
            break
        if not topic:
            continue
        print(chain.invoke({"topic": topic}))


def run() -> None:
    model = chat_model()
    lesson_model_call(model)
    lesson_prompt_template()
    lesson_chain(model)
    practice(model)


if __name__ == "__main__":
    run()
