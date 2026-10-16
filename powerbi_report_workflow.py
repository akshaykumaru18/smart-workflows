"""Ask Gemini to upload a Power BI semantic model.

Sign-in uses the device-code flow from authv2_powerbi.py.
The first successful sign-in is saved in .powerbi_token_cache.json and reused on later runs.

Export these before running:
    export POWERBI_CLIENT_ID="your-app-client-id"
    export POWERBI_TENANT_ID="your-tenant-id"
    export GEMINI_API_KEY="your-gemini-key"
    python powerbi_report_workflow.py
"""

import inspect
import json
import os

from google import genai
from google.genai import types

from tools import (
    append_file,
    create_directory,
    create_file,
    create_push_dataset,
    create_workspace,
    export_report,
    extract_report_definition,
    get_device_access_token,
    get_report,
    get_report_pages,
    list_reports,
    list_workspaces,
    read_file,
    replace_in_file,
    update_file,
)


MODEL = "gemini-flash-lite-latest"
MAX_TOOL_CALLS = 8
SCOPES = [
    "https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Report.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Content.Create",
    "https://analysis.windows.net/powerbi/api/Workspace.ReadWrite.All",
]

message_history = []
client = None
system_prompt = ""
tools = {}


def tool_catalog() -> str:
    lines = []
    for name, function in tools.items():
        description = (function.__doc__ or "").strip()
        lines.append(f"- {name}{inspect.signature(function)}: {description}")
    return "\n".join(lines)


def parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def call_tool(data):
    if isinstance(data, str):
        name, arguments = data, {}
    elif isinstance(data, dict):
        name = data.get("tool", data.get("name"))
        arguments = data.get("arguments") or {}
    else:
        raise KeyError("data")
    if name not in tools or not isinstance(arguments, dict):
        raise KeyError(name)
    return tools[name](**arguments)


def gemini_tool(user_prompt: str) -> str:
    print("Gemini Tool Accessed")
    message_history.append({"role": "user", "parts": [{"text": user_prompt}]})
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
    message_history.append({"role": "model", "parts": [{"text": response.text}]})
    return response.text


def run():
    global client, system_prompt, tools, message_history
    required = ("POWERBI_CLIENT_ID", "POWERBI_TENANT_ID", "GEMINI_API_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return

    token = get_device_access_token(
        os.environ["POWERBI_TENANT_ID"],
        os.environ["POWERBI_CLIENT_ID"],
        SCOPES,
    )
    workspaces = list_workspaces(token)
    if not workspaces:
        workspace = create_workspace(token, "Smart Workflows")
        print(f"Created workspace: {workspace['name']}")
    else:
        workspace = workspaces[0]
    workspace_id = workspace["id"]
    workspace_name = workspace["name"]
    print(f"Using workspace: {workspace_name}")

    def upload_semantic_model(name: str, rows: list) -> dict:
        """Upload a semantic model and its rows into the current Power BI workspace. Arguments: name, rows."""
        result = create_push_dataset(token, workspace_id, name, rows)
        print(
            f"Semantic model '{result['dataset_name']}' is in Power BI workspace "
            f"'{workspace_name}': {result['web_url']}"
        )
        return result

    def reports() -> list:
        """List reports in this workspace. No arguments."""
        return list_reports(token, workspace_id)

    def report_metadata(report_id: str) -> dict:
        """Return report metadata: name, id, semantic-model id, and URL. Arguments: report_id."""
        return get_report(token, workspace_id, report_id)

    def report_pages(report_id: str) -> dict:
        """Return each report page and its visuals. Arguments: report_id."""
        return get_report_pages(token, workspace_id, report_id)

    def save_report_details(report_id: str) -> dict:
        """Save report metadata and page details as a readable JSON file. Arguments: report_id."""
        result = export_report(token, workspace_id, report_id)
        print(f"Saved report details: {result['path']}")
        return result

    def extract_report(report_id: str, output_dir: str | None = None) -> dict:
        """Save a report's Fabric definition files. Arguments: report_id, optional output_dir."""
        fabric_token = os.environ.get("FABRIC_ACCESS_TOKEN") or get_device_access_token(
            os.environ["POWERBI_TENANT_ID"],
            os.environ["POWERBI_CLIENT_ID"],
            ["https://api.fabric.microsoft.com/.default"],
        )
        result = extract_report_definition(fabric_token, workspace_id, report_id, output_dir)
        print(f"Saved {result['part_count']} definition parts in {result['output_dir']}")
        return result

    tools = {
        "read_file": read_file,
        "create_file": create_file,
        "create_directory": create_directory,
        "update_file": update_file,
        "append_file": append_file,
        "replace_in_file": replace_in_file,
        "upload_semantic_model": upload_semantic_model,
        "list_reports": reports,
        "get_report": report_metadata,
        "get_report_pages": report_pages,
        "save_report_details": save_report_details,
        "extract_report_definition": extract_report,
    }
    system_prompt = f"""
Upload a semantic model for the user's question in workspace "{workspace_name}".
Do not create or import a .pbix file.

You can call only the tools listed below. Call one tool at a time.

- read_file(path): read a local text file. Use this first when the question names a file, then build rows from its contents.
- create_file(path, content): create a new text file and any missing parent folders.
- create_directory(path): create a directory and any missing parent folders.
- update_file(path, content): replace the full contents of an existing text file.
- append_file(path, content): add text to the end of an existing file.
- replace_in_file(path, old, new): replace every occurrence of old text with new text in an existing file.
- upload_semantic_model(name, rows): upload the semantic model into this Power BI workspace. rows is a list of objects. This upload is the refresh, so the rows are the current data. The result includes workspace_id, dataset_id, and web_url.
- list_reports(): list reports in this workspace. A dataset id is not a report id.
- get_report(report_id): return report metadata: name, id, semantic-model id, and URL. Use a report id from list_reports.
- get_report_pages(report_id): return each page and its visuals.
- save_report_details(report_id): save the report metadata and page details as a readable JSON file under reports/. Do not save a .pbix file.
- extract_report_definition(report_id, output_dir): download the Fabric report definition and write each part under output_dir. output_dir is optional and defaults to reports/<report_id>-definition. Use a report id from list_reports.

When the question depends on a local file, call read_file before upload_semantic_model.
When no file is given and the user wants new data uploaded, call upload_semantic_model with a short name and 4 dummy rows that match the question.
When the user asks to read or save a report, call list_reports first, then get_report, get_report_pages, or save_report_details with a report id from that list.
Use a file-writing tool only when the question asks to create or change a local file.

Reply with only this JSON object:
{{
  "reprompt": false,
  "data_needed": true,
  "data": {{"tool": "tool_name", "arguments": {{}}}},
  "response": ""
}}

Set data.tool to one tool name from the list above.
Use reprompt=true only when the question is unclear, and put the question in response.
After upload_semantic_model succeeds, answer with data_needed=false, data=null, reprompt=false.
Copy the dataset id, dataset name, row count, workspace name, and web_url from the tool result. Do not invent them.
After save_report_details succeeds, answer the same way and copy the saved JSON path, report name, and page count.

Tool signatures:
{tool_catalog()}
"""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
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
                    snapshot = call_tool(response_json["data"])
                except KeyError:
                    print("Invalid data")
                    break
                except TypeError as error:
                    print(f"Invalid arguments: {error}")
                    break
                except Exception as error:
                    print(error)
                    break

                user_prompt = (
                    f"User question: {user_question}\n"
                    f"Required data: {json.dumps(snapshot, default=str)}\n"
                    "Continue the tool sequence. Read or write files when still needed. "
                    "When the requested upload or report save is finished, answer in valid JSON "
                    "with data_needed=false, data=null, reprompt=false, "
                    "and response copying ids, names, and the saved JSON path from the tool result."
                )
        except (json.JSONDecodeError, KeyError) as error:
            print(f"Invalid response: {error}")


if __name__ == "__main__":
    run()
