"""Publish a CSV to Power BI, then query it with DAX or export the report.

Sign-in uses a service principal when POWERBI_CLIENT_SECRET is set, and a
device code otherwise. The app needs permission to create datasets and reports.

Export these before running:
    export POWERBI_TENANT_ID="your-tenant-id"
    export POWERBI_CLIENT_ID="your-app-client-id"
    export POWERBI_CLIENT_SECRET="your-client-secret"
    export POWERBI_WORKSPACE_ID="your-workspace-id"
    export GEMINI_API_KEY="your-gemini-key"
    python powerbi_workflow.py

POWERBI_WORKSPACE_ID is optional. Without it, ask the workflow to list
workspaces and pass a workspace id. Execute Queries and Export To File need
the workspace on a Premium, PPU, Embedded, or Fabric capacity.
"""

import inspect
import json
import os

from google import genai
from google.genai import types

from tools.file_tools import (
    append_file,
    create_directory,
    create_file,
    read_file,
    replace_in_file,
    update_file,
)
from tools.powerbi_tools import (
    create_workspace,
    execute_dax,
    export_report_file,
    get_access_token,
    get_dataset,
    get_device_access_token,
    get_report,
    get_report_pages,
    list_datasets,
    list_reports,
    list_workspaces,
    publish_powerbi_report,
    refresh_dataset,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


MODEL = "gemini-flash-lite-latest"
MAX_TOOL_CALLS = 8
SCOPES = [
    "https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Report.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Workspace.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Dashboard.Read.All",
    "https://analysis.windows.net/powerbi/api/Content.Create",
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
    message_history.append({"role": "model", "parts": [{"text": response.text}]})
    return response.text


def workspace_id(value: str | None) -> str:
    chosen = value or os.getenv("POWERBI_WORKSPACE_ID")
    if not chosen:
        raise ValueError(
            "workspace_id is required. Call list_workspaces, or set POWERBI_WORKSPACE_ID."
        )
    return chosen


def run():
    global client, system_prompt, tools, message_history
    if load_dotenv is not None:
        load_dotenv()

    required = ("POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "GEMINI_API_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return

    if os.environ.get("POWERBI_CLIENT_SECRET"):
        token = get_access_token(
            os.environ["POWERBI_TENANT_ID"],
            os.environ["POWERBI_CLIENT_ID"],
            os.environ["POWERBI_CLIENT_SECRET"],
        )
        print("Signed in to Power BI with the service principal.")
    else:
        token = get_device_access_token(
            os.environ["POWERBI_TENANT_ID"],
            os.environ["POWERBI_CLIENT_ID"],
            SCOPES,
        )
        print("Signed in to Power BI.")

    def workspaces() -> list:
        """List Power BI workspaces. No arguments."""
        return list_workspaces(token)

    def new_workspace(name: str) -> dict:
        """Create a Power BI workspace. Arguments: name."""
        return create_workspace(token, name)

    def datasets(workspace: str | None = None) -> list:
        """List datasets in a workspace. Arguments: optional workspace."""
        return list_datasets(token, workspace_id(workspace))

    def dataset(dataset_id: str, workspace: str | None = None) -> dict:
        """Get one dataset. Arguments: dataset_id, optional workspace."""
        return get_dataset(token, workspace_id(workspace), dataset_id)

    def reports(workspace: str | None = None) -> list:
        """List reports in a workspace. Arguments: optional workspace."""
        return list_reports(token, workspace_id(workspace))

    def report(report_id: str, workspace: str | None = None) -> dict:
        """Get one report. Arguments: report_id, optional workspace."""
        return get_report(token, workspace_id(workspace), report_id)

    def report_pages(report_id: str, workspace: str | None = None) -> dict:
        """List pages on a report. Arguments: report_id, optional workspace."""
        return get_report_pages(token, workspace_id(workspace), report_id)

    def refresh(dataset_id: str, workspace: str | None = None) -> dict:
        """Start a dataset refresh. This does not reload a CSV. Arguments: dataset_id, optional workspace."""
        refresh_dataset(token, workspace_id(workspace), dataset_id)
        return {"dataset_id": dataset_id, "refresh": "started"}

    def publish(
        file_path: str,
        workspace: str | None = None,
        name: str | None = None,
        chart: str | None = None,
        dimension: str | None = None,
        measure: str | None = None,
    ) -> dict:
        """Publish a CSV. Pages and DAX measures come from that file's columns. Arguments: file_path, optional workspace, name, chart, dimension, measure."""
        result = publish_powerbi_report(
            token,
            workspace_id(workspace),
            file_path,
            name,
            chart,
            dimension,
            measure,
        )
        print(
            f"Published {result.get('name')} ({result.get('row_count')} rows from {file_path}). "
            f"Pages: {', '.join(result.get('pages') or [])}. "
            f"Report: {result.get('report_url')}"
        )
        return result

    def run_dax(dataset_id: str, query: str, workspace: str | None = None) -> dict:
        """Run a DAX query. Arguments: dataset_id, query, optional workspace. Example query: EVALUATE ROW("Row Count", [Row Count])."""
        return execute_dax(token, workspace_id(workspace), dataset_id, query)

    def export_file(
        report_id: str,
        file_format: str = "PDF",
        workspace: str | None = None,
        output_path: str | None = None,
    ) -> dict:
        """Export a report to PDF, PNG, or PPTX. Arguments: report_id, optional file_format, workspace, output_path."""
        result = export_report_file(
            token,
            workspace_id(workspace),
            report_id,
            file_format,
            output_path,
        )
        print(f"Exported {result.get('format')} to {result.get('path')}")
        return result

    tools = {
        "read_file": read_file,
        "create_file": create_file,
        "create_directory": create_directory,
        "update_file": update_file,
        "append_file": append_file,
        "replace_in_file": replace_in_file,
        "list_workspaces": workspaces,
        "create_workspace": new_workspace,
        "list_datasets": datasets,
        "get_dataset": dataset,
        "list_reports": reports,
        "get_report": report,
        "get_report_pages": report_pages,
        "refresh_dataset": refresh,
        "publish_powerbi_report": publish,
        "execute_dax": run_dax,
        "export_report_file": export_file,
    }
    system_prompt = f"""
Help the user with local files and the signed-in Power BI account.

You can call only the tools listed below. Call one tool at a time.

- read_file(path): read a local text file.
- create_file(path, content): create a new text file and any missing parent folders.
- create_directory(path): create a directory and any missing parent folders.
- update_file(path, content): replace the full contents of an existing text file.
- append_file(path, content): add text to the end of an existing file.
- replace_in_file(path, old, new): replace every occurrence of old text with new text in an existing file.
- list_workspaces(): list workspaces.
- create_workspace(name): create a workspace.
- list_datasets(workspace): list datasets. workspace is optional when POWERBI_WORKSPACE_ID is set.
- get_dataset(dataset_id, workspace): get one dataset.
- list_reports(workspace): list reports.
- get_report(report_id, workspace): get one report.
- get_report_pages(report_id, workspace): list report pages.
- refresh_dataset(dataset_id, workspace): start a refresh. This does not reload the user's CSV.
- publish_powerbi_report(file_path, workspace, name): read the user's CSV, push the rows, create DAX measures, and publish a report whose pages come from the columns. name and workspace are optional. Leave chart, dimension, and measure empty.
- execute_dax(dataset_id, query, workspace): run a DAX query. Use a measure name returned by publish. Example: EVALUATE ROW("Row Count", [Row Count]).
- export_report_file(report_id, file_format, workspace, output_path): save the report as PDF, PNG, or PPTX. file_format defaults to PDF.

A refresh does not add the user's rows. The rows are loaded by publish_powerbi_report.
When the user wants their own data in Power BI, call read_file on the file path they gave, then call publish_powerbi_report with that file_path only. The tool reads the header and values. It adds a Row Count measure and a Total measure for each numeric column used on a page. It builds a bar for each numeric column by a category, and a line for each numeric column over a date. Different files produce different pages. Pass chart, dimension, and measure only when the user names a chart and those exact column names appear in the file you just read.
When the user asks for a calculation, call execute_dax with a measure name from the publish result. Do not invent measure names.
When the user asks for a PDF or image, call export_report_file with the report id from publish.
Never create a sample, dummy, or substitute file. If a tool returns an error, copy that error into the response and stop.
Use a file tool when the question asks to read or change a local file.

Reply with only this JSON object:
{{
  "reprompt": false,
  "data_needed": true,
  "data": {{"tool": "tool_name", "arguments": {{}}}},
  "response": ""
}}

Set data.tool to one tool name from the list above.
Use reprompt=true only when the question is unclear, and put the question in response.
When the requested work is done, answer with data_needed=false, data=null, reprompt=false,
and copy ids, names, measure expressions, page names, and file paths from the tool result. Do not invent them.

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
                except Exception as error:
                    print(error)
                    snapshot = {"error": str(error)}

                user_prompt = (
                    f"User question: {user_question}\n"
                    f"Required data: {json.dumps(snapshot, default=str)}\n"
                    "Continue the tool sequence. Read or write files when still needed. "
                    "If this result is the user's file and they want it in Power BI, call "
                    "publish_powerbi_report with that file path only. Leave chart, dimension, and "
                    "measure empty so the pages follow the columns in the file. "
                    "If they asked for a calculation, call execute_dax with a measure name from the "
                    "publish result. Example: EVALUATE ROW(\"Row Count\", [Row Count]). "
                    "If they asked for a PDF or image, call export_report_file with the report id. "
                    "If the tool returned an error, correct the arguments only when they were wrong. "
                    "Do not create a sample or dummy file. "
                    "When the requested Power BI work is done, answer in valid JSON "
                    "with data_needed=false, data=null, reprompt=false, "
                    "and response copying names, ids, pages, measure expressions, file paths, and query rows from the tool result."
                )
        except (json.JSONDecodeError, KeyError) as error:
            print(f"Invalid response: {error}")


if __name__ == "__main__":
    run()
