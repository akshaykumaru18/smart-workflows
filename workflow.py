"""Reusable question loop.

Copy this file for a new objective. Change OBJECTIVE, MODEL, and TOOLS.
The loop stays the same.

Set GEMINI_API_KEY before running:
    python workflow.py
"""

import inspect
import json
import os

from google import genai
from google.genai import types

from tools import (
    api_call,
    append_file,
    create_directory,
    create_file,
    get_access_token,
    get_dashboard,
    get_dataset,
    get_report,
    get_workspace,
    list_dashboards,
    list_datasets,
    list_reports,
    list_workspaces,
    read_file,
    refresh_dataset,
    replace_in_file,
    send_gmail,
    update_file,
)

# --- change these ---
MODEL = "gemini-flash-lite-latest"
MAX_TOOL_CALLS = 5
OBJECTIVE = "Help the user with files, API calls, Gmail, and Power BI."

TOOLS = {
    "create_file": create_file,
    "create_directory": create_directory,
    "read_file": read_file,
    "update_file": update_file,
    "append_file": append_file,
    "replace_in_file": replace_in_file,
    "api_call": api_call,
    "send_gmail": send_gmail,
    "get_access_token": get_access_token,
    "list_workspaces": list_workspaces,
    "get_workspace": get_workspace,
    "list_datasets": list_datasets,
    "get_dataset": get_dataset,
    "refresh_dataset": refresh_dataset,
    "list_reports": list_reports,
    "get_report": get_report,
    "list_dashboards": list_dashboards,
    "get_dashboard": get_dashboard,
}
# --- loop below stays the same ---

message_history = []
client = None
system_prompt = ""


def tool_catalog(tools: dict) -> str:
    lines = []
    for name, function in tools.items():
        description = (function.__doc__ or "").strip()
        lines.append(f"- {name}{inspect.signature(function)}: {description}")
    return "\n".join(lines)


def build_system_prompt(objective: str, tools: dict) -> str:
    return f"""{objective}

Reply with only this JSON object:
{{
  "reprompt": false,
  "data_needed": false,
  "data": null,
  "response": ""
}}

Use reprompt=true when the question is unclear. Put the clarifying question in response.
Use data_needed=true when you need a tool. Set data to {{"tool": "tool_name", "arguments": {{}}}}.
A tool name string is also valid when the tool takes no arguments.
Use data_needed=false when you can answer. Put the short answer in response.

Tools:
{tool_catalog(tools)}
"""


def parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def call_registered_tool(data):
    if isinstance(data, str):
        name, arguments = data, {}
    elif isinstance(data, dict):
        name = data.get("tool", data.get("name"))
        arguments = data.get("arguments") or {}
    else:
        raise KeyError("data")

    if not isinstance(name, str) or name not in TOOLS:
        raise KeyError(name)
    if not isinstance(arguments, dict):
        raise KeyError("arguments")
    return TOOLS[name](**arguments)


def gemini_tool(user_prompt: str) -> str:
    print("Gemini Tool Accessed")

    message_history.append({
        "role": "user",
        "parts": [{"text": user_prompt}],
    })

    response = client.models.generate_content(
        model=MODEL,
        contents=message_history,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
        ),
    )

    print("--------------------------------")
    print(response.usage_metadata)
    print("--------------------------------")

    message_history.append({
        "role": "model",
        "parts": [{"text": response.text}],
    })
    return response.text


def run():
    global client, system_prompt, message_history

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    system_prompt = build_system_prompt(OBJECTIVE, TOOLS)
    message_history = []

    while True:
        user_question = input("Enter your query: ").strip()
        if user_question.lower() in ["exit", "quit"]:
            break
        if not user_question:
            continue

        user_prompt = f"User question: {user_question}. Keep the answer short and concise."

        try:
            for step in range(MAX_TOOL_CALLS + 1):
                response_json = parse_json(gemini_tool(user_prompt))
                print(response_json)

                if response_json["reprompt"]:
                    print(response_json.get("response", "Please clarify your question."))
                    break

                if not response_json["data_needed"]:
                    print(json.dumps(response_json, indent=2))
                    break

                if step == MAX_TOOL_CALLS:
                    print("Too many tool calls.")
                    break

                try:
                    snapshot = call_registered_tool(response_json["data"])
                except KeyError:
                    print("Invalid data")
                    break
                except TypeError as error:
                    print(f"Invalid arguments: {error}")
                    break

                user_prompt = (
                    f"User question: {user_question}\n"
                    f"Required data: {json.dumps(snapshot, default=str)}\n"
                    "Analyze the data and answer concisely in valid JSON "
                    "with data_needed=false, data=null, reprompt=false, "
                    "and response containing the answer."
                )
        except (json.JSONDecodeError, KeyError) as error:
            print(f"Invalid response: {error}")


if __name__ == "__main__":
    run()
