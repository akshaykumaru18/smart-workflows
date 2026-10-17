"""Ask Gemini to use Tableau's official MCP tools.

This is a copy of tableau_workflow.py. Tableau work is done by the
Tableau MCP server (@tableau/mcp-server). This script does not define
those Tableau tools. It starts the server, reads the tool list, and
calls whatever the server exposes.

Local file tools stay in this process. Tableau MCP does not read or
write files on this machine.

Sign-in uses the same connected-app variables as tableau_workflow.py.
The MCP server turns them into a scoped JWT (AUTH=direct-trust).

Export these before running:
    export TABLEAU_SERVER="https://your-pod.online.tableau.com"
    export TABLEAU_SITE_CONTENT_URL="your-site"
    export TABLEAU_CLIENT_ID="your-connected-app-client-id"
    export TABLEAU_SECRET_ID="your-secret-id"
    export TABLEAU_SECRET_VALUE="your-secret-value"
    export TABLEAU_USERNAME="your-tableau-username"
    export GEMINI_API_KEY="your-gemini-key"
    python tableau_mcp_workflow.py

Optional:
    TABLEAU_MCP_AUTH=pat
    TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE
    TABLEAU_MCP_INCLUDE_TOOLS=workbook,view,datasource,project,jobs,tasks
    TABLEAU_MCP_COMMAND=npx

Node.js 22.7.5 or newer must be on PATH. The first run downloads
@tableau/mcp-server.
"""

import asyncio
import json
import os
import shutil
from datetime import timedelta

from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tools.file_tools import (
    append_file,
    create_directory,
    create_file,
    read_file,
    replace_in_file,
    update_file,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


MODEL = "gemini-flash-lite-latest"
MAX_TOOL_CALLS = 8
MAX_TOOL_TEXT = 12000
DEFAULT_INCLUDE_TOOLS = (
    "workbook,view,datasource,project,jobs,tasks,authoring,content-exploration"
)

FILE_TOOLS = {
    "read_file": read_file,
    "create_file": create_file,
    "create_directory": create_directory,
    "update_file": update_file,
    "append_file": append_file,
    "replace_in_file": replace_in_file,
}


def failure_text(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(failure_text(item) for item in error.exceptions)
    return str(error)


def missing_environment() -> list[str]:
    auth = os.getenv("TABLEAU_MCP_AUTH", "direct-trust")
    required = ["TABLEAU_SERVER", "TABLEAU_SITE_CONTENT_URL", "GEMINI_API_KEY"]
    if auth == "pat":
        required.extend(["TABLEAU_PAT_NAME", "TABLEAU_PAT_VALUE"])
    elif auth == "direct-trust":
        required.extend(
            [
                "TABLEAU_CLIENT_ID",
                "TABLEAU_SECRET_ID",
                "TABLEAU_SECRET_VALUE",
                "TABLEAU_USERNAME",
            ]
        )
    missing = []
    for name in required:
        if name not in os.environ:
            missing.append(name)
        elif name != "TABLEAU_SITE_CONTENT_URL" and not os.environ[name]:
            missing.append(name)
    if auth not in {"pat", "direct-trust"}:
        missing.append("TABLEAU_MCP_AUTH (use direct-trust or pat)")
    return missing


def mcp_environment() -> dict[str, str]:
    auth = os.getenv("TABLEAU_MCP_AUTH", "direct-trust")
    environment = {
        "SERVER": os.environ["TABLEAU_SERVER"],
        "SITE_NAME": os.environ["TABLEAU_SITE_CONTENT_URL"],
        "AUTH": auth,
        "TRANSPORT": "stdio",
        "LOG_LEVEL": os.getenv("TABLEAU_MCP_LOG_LEVEL", "warning"),
        "INCLUDE_TOOLS": os.getenv("TABLEAU_MCP_INCLUDE_TOOLS", DEFAULT_INCLUDE_TOOLS),
    }
    if auth == "direct-trust":
        environment.update(
            {
                "JWT_SUB_CLAIM": os.environ["TABLEAU_USERNAME"],
                "CONNECTED_APP_CLIENT_ID": os.environ["TABLEAU_CLIENT_ID"],
                "CONNECTED_APP_SECRET_ID": os.environ["TABLEAU_SECRET_ID"],
                "CONNECTED_APP_SECRET_VALUE": os.environ["TABLEAU_SECRET_VALUE"],
            }
        )
    if auth == "pat":
        environment["PAT_NAME"] = os.environ["TABLEAU_PAT_NAME"]
        environment["PAT_VALUE"] = os.environ["TABLEAU_PAT_VALUE"]
    return environment


def server_parameters() -> StdioServerParameters:
    command = os.getenv("TABLEAU_MCP_COMMAND", "npx")
    resolved = shutil.which(command)
    if resolved is None:
        raise FileNotFoundError(
            f"{command} was not found on PATH. Install Node.js 22.7.5 or newer."
        )
    args = os.getenv("TABLEAU_MCP_ARGS", "-y @tableau/mcp-server@latest").split()
    return StdioServerParameters(command=resolved, args=args, env=mcp_environment())


def argument_summary(schema: dict) -> str:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not properties:
        return "no arguments"
    required = set(schema.get("required") or [])
    parts = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            parts.append(name)
            continue
        kind = spec.get("type") or "value"
        if spec.get("enum"):
            kind = "one of " + ", ".join(str(item) for item in spec["enum"])
        note = "required" if name in required else "optional"
        description = (spec.get("description") or "").strip().replace("\n", " ")
        if len(description) > 180:
            description = description[:177] + "..."
        piece = f"{name} ({kind}, {note})"
        if description:
            piece += f": {description}"
        parts.append(piece)
    return "; ".join(parts)


def file_tool_catalog() -> str:
    return "\n".join(
        [
            "- read_file(path): read a local text file.",
            "- create_file(path, content): create a new text file and any missing parent folders.",
            "- create_directory(path): create a directory and any missing parent folders.",
            "- update_file(path, content): replace the full contents of an existing text file.",
            "- append_file(path, content): add text to the end of an existing file.",
            "- replace_in_file(path, old, new): replace every occurrence of old text with new text.",
        ]
    )


def tableau_tool_catalog(tools: list) -> str:
    lines = []
    for tool in tools:
        description = (tool.description or "").strip().replace("\n", " ")
        lines.append(
            f"- {tool.name}: {description} Arguments: {argument_summary(tool.inputSchema)}."
        )
    return "\n".join(lines)


def system_prompt_for(tableau_tools: list) -> str:
    names = ", ".join(tool.name for tool in tableau_tools)
    return f"""
Help the user with local files and the Tableau site behind Tableau MCP.

You can call only the tools listed below. Call one tool at a time.
Tableau tools are provided by Tableau's MCP server. Use each name exactly as written.
Do not call publish_sales_workbook, refresh_workbook, refresh_datasource, sign_in, or sign_out.
Those belong to the other workflow and are not available here.

Local file tools:
{file_tool_catalog()}

Tableau MCP tools:
{tableau_tool_catalog(tableau_tools)}

Use a file tool when the question asks to read or change a local file.
Use a Tableau MCP tool when the question asks about workbooks, views, data sources, projects, jobs, or tasks.
This server does not build a workbook from CSV columns. If the user wants that, say this workflow cannot do it.
Never create a sample, dummy, or substitute file. If a tool returns an error, copy that error into the response and stop.

Reply with only this JSON object:
{{
  "reprompt": false,
  "data_needed": true,
  "data": {{"tool": "tool_name", "arguments": {{}}}},
  "response": ""
}}

Set data.tool to one of: {names}, read_file, create_file, create_directory, update_file, append_file, replace_in_file.
Use reprompt=true only when the question is unclear, and put the question in response.
When the requested work is done, answer with data_needed=false, data=null, reprompt=false,
and copy ids, names, and job status from the tool result. Do not invent them.
"""


def parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def shrink_tool_result(payload: dict) -> dict:
    content = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            content.append(block)
            continue
        if block.get("type") == "image":
            raw = block.get("data") or ""
            content.append(
                {
                    "type": "image",
                    "mimeType": block.get("mimeType"),
                    "bytes": len(raw) * 3 // 4,
                }
            )
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            if len(text) > MAX_TOOL_TEXT:
                text = text[:MAX_TOOL_TEXT] + "\n...truncated..."
            content.append({"type": "text", "text": text})
            continue
        content.append(block)
    payload["content"] = content
    return payload


async def listed_tools(session: ClientSession) -> list:
    tools = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


async def call_named_tool(session: ClientSession, name: str, arguments: dict):
    if name in FILE_TOOLS:
        return FILE_TOOLS[name](**arguments)
    if not isinstance(arguments, dict):
        raise TypeError("arguments must be an object")
    result = await session.call_tool(
        name,
        arguments=arguments,
        read_timeout_seconds=timedelta(minutes=3),
    )
    return shrink_tool_result(result.model_dump(mode="json", exclude_none=True))


def ask_gemini(client, history: list, prompt: str, instructions: str) -> str:
    print("Gemini Tool Accessed")
    history.append({"role": "user", "parts": [{"text": prompt}]})
    response = client.models.generate_content(
        model=MODEL,
        contents=history,
        config=types.GenerateContentConfig(
            system_instruction=instructions,
            response_mime_type="application/json",
        ),
    )
    history.append({"role": "model", "parts": [{"text": response.text}]})
    return response.text


async def conversation(session: ClientSession, tableau_tools: list) -> None:
    names = {tool.name for tool in tableau_tools}
    instructions = system_prompt_for(tableau_tools)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    history = []

    while True:
        try:
            user_question = (await asyncio.to_thread(input, "Enter your query: ")).strip()
        except EOFError:
            break
        if user_question.lower() in {"exit", "quit"}:
            break
        if not user_question:
            continue

        user_prompt = f"User question: {user_question}. Keep the answer short and concise."
        try:
            for step in range(MAX_TOOL_CALLS + 1):
                response_json = parse_json(ask_gemini(client, history, user_prompt, instructions))
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

                data = response_json["data"]
                if isinstance(data, str):
                    name, arguments = data, {}
                elif isinstance(data, dict):
                    name = data.get("tool", data.get("name"))
                    arguments = data.get("arguments") or {}
                else:
                    name, arguments = None, {}

                if name not in names and name not in FILE_TOOLS:
                    snapshot = {"error": f"Unknown tool: {name}"}
                else:
                    try:
                        snapshot = await call_named_tool(session, name, arguments)
                    except Exception as error:
                        print(error)
                        snapshot = {"error": str(error)}

                user_prompt = (
                    f"User question: {user_question}\n"
                    f"Required data: {json.dumps(snapshot, default=str)}\n"
                    "Continue only if another listed tool is still required. "
                    "If the tool returned an error, copy that error and stop. "
                    "Do not create a sample or dummy file. "
                    "When the requested work is done, answer in valid JSON "
                    "with data_needed=false, data=null, reprompt=false, "
                    "and response copying names, ids, and job status from the tool result."
                )
        except (json.JSONDecodeError, KeyError) as error:
            print(f"Invalid response: {error}")


async def run() -> None:
    if load_dotenv is not None:
        load_dotenv()

    missing = missing_environment()
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return

    try:
        parameters = server_parameters()
    except FileNotFoundError as error:
        print(error)
        return

    print("Starting Tableau MCP. The first run downloads @tableau/mcp-server.", flush=True)
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tableau_tools = await listed_tools(session)
                print(
                    "Tableau MCP tools: "
                    + ", ".join(tool.name for tool in tableau_tools),
                    flush=True,
                )
                await conversation(session, tableau_tools)
    except (Exception, BaseExceptionGroup) as error:
        print(f"Tableau MCP failed: {failure_text(error)}")
        print("Check TABLEAU_SERVER, the site content URL, and the connected-app values.")
        return
    print("Tableau MCP stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
