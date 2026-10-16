"""Ask Gemini to list and refresh Tableau workbooks and data sources.

Sign-in uses a Tableau connected-app JWT. The connected app must allow
tableau:content:read and tableau:tasks:run.

Export these before running:
    export TABLEAU_SERVER="https://your-pod.online.tableau.com"
    export TABLEAU_SITE_CONTENT_URL="your-site"
    export TABLEAU_CLIENT_ID="your-connected-app-client-id"
    export TABLEAU_SECRET_ID="your-secret-id"
    export TABLEAU_SECRET_VALUE="your-secret-value"
    export TABLEAU_USERNAME="your-tableau-username"
    export GEMINI_API_KEY="your-gemini-key"
    python tableau_workflow.py

TABLEAU_API_VERSION defaults to 3.29.
TABLEAU_SCOPES defaults to tableau:content:read,tableau:tasks:run.
Publishing a workbook also needs tableau:datasources:create, tableau:workbooks:create,
and tableau:views:download enabled on the connected app and included in TABLEAU_SCOPES.
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
    get_job,
    get_view_data,
    get_view_image,
    get_view_pdf,
    list_datasources,
    list_projects,
    list_views,
    list_workbooks,
    publish_sales_workbook,
    read_file,
    refresh_datasource,
    refresh_workbook,
    replace_in_file,
    sign_in,
    sign_out,
    update_file,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


MODEL = "gemini-flash-lite-latest"
MAX_TOOL_CALLS = 8

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
    # print("--------------------------------")
    # print(response.usage_metadata)
    # print("--------------------------------")
    message_history.append({"role": "model", "parts": [{"text": response.text}]})
    return response.text


def run():
    global client, system_prompt, tools, message_history
    if load_dotenv is not None:
        load_dotenv()

    required = (
        "TABLEAU_SERVER",
        "TABLEAU_SITE_CONTENT_URL",
        "TABLEAU_CLIENT_ID",
        "TABLEAU_SECRET_ID",
        "TABLEAU_SECRET_VALUE",
        "TABLEAU_USERNAME",
        "GEMINI_API_KEY",
    )
    missing = []
    for name in required:
        if name not in os.environ:
            missing.append(name)
        elif name != "TABLEAU_SITE_CONTENT_URL" and not os.environ[name]:
            missing.append(name)
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return

    scopes = [
        scope.strip()
        for scope in os.getenv("TABLEAU_SCOPES", "tableau:content:read,tableau:tasks:run").split(",")
        if scope.strip()
    ]
    session = sign_in(
        os.environ["TABLEAU_SERVER"],
        os.environ["TABLEAU_SITE_CONTENT_URL"],
        os.environ["TABLEAU_CLIENT_ID"],
        os.environ["TABLEAU_SECRET_ID"],
        os.environ["TABLEAU_SECRET_VALUE"],
        os.environ["TABLEAU_USERNAME"],
        os.getenv("TABLEAU_API_VERSION", "3.29"),
        scopes,
    )
    print(f"Signed in to Tableau site {session['site_id']}")

    try:
        def workbooks() -> list:
            """List Tableau workbooks. No arguments."""
            return list_workbooks(session)

        def datasources() -> list:
            """List published Tableau data sources. No arguments."""
            return list_datasources(session)

        def refresh_existing_workbook(workbook_id: str) -> dict:
            """Refresh the extract for an existing workbook. Arguments: workbook_id."""
            result = refresh_workbook(session, workbook_id)
            print(f"Workbook refresh job {result.get('id')} started.")
            return result

        def refresh_existing_datasource(datasource_id: str) -> dict:
            """Refresh the extract for an existing data source. Arguments: datasource_id."""
            result = refresh_datasource(session, datasource_id)
            print(f"Data source refresh job {result.get('id')} started.")
            return result

        def job_status(job_id: str) -> dict:
            """Check a Tableau refresh job. Arguments: job_id."""
            return get_job(session, job_id)

        def views(workbook_id: str | None = None) -> list:
            """List Tableau views. Optional argument: workbook_id."""
            return list_views(session, workbook_id or None)

        def view_data(view_id: str, max_rows: int = 500) -> dict:
            """Read CSV data from a view. Arguments: view_id, max_rows."""
            return get_view_data(session, view_id, max_rows)

        def view_image(view_id: str, output_path: str) -> dict:
            """Download a view as PNG. Arguments: view_id, output_path."""
            return get_view_image(session, view_id, output_path)

        def view_pdf(view_id: str, output_path: str) -> dict:
            """Download a view as PDF. Arguments: view_id, output_path."""
            return get_view_pdf(session, view_id, output_path)

        def projects() -> list:
            """List Tableau projects. No arguments."""
            return list_projects(session)

        def publish_sales(file_path: str, name: str | None = None) -> dict:
            """Read a local CSV through the file tools and publish those rows with a bar chart. Arguments: file_path, optional name."""
            result = publish_sales_workbook(session, file_path, name)
            workbook = result.get("workbook") or {}
            print(
                f"Published {result.get('name')} ({result.get('row_count')} rows from {file_path}) "
                f"to Tableau: {workbook.get('webpage_url') or workbook.get('id')}"
            )
            return result

        tools = {
            "read_file": read_file,
            "create_file": create_file,
            "create_directory": create_directory,
            "update_file": update_file,
            "append_file": append_file,
            "replace_in_file": replace_in_file,
            "list_projects": projects,
            "list_workbooks": workbooks,
            "list_views": views,
            "list_datasources": datasources,
            "get_view_data": view_data,
            "get_view_image": view_image,
            "get_view_pdf": view_pdf,
            "refresh_workbook": refresh_existing_workbook,
            "refresh_datasource": refresh_existing_datasource,
            "get_job": job_status,
            "publish_sales_workbook": publish_sales,
        }
        system_prompt = f"""
Help the user with local files and the signed-in Tableau site.

You can call only the tools listed below. Call one tool at a time.

- read_file(path): read a local text file.
- create_file(path, content): create a new text file and any missing parent folders.
- create_directory(path): create a directory and any missing parent folders.
- update_file(path, content): replace the full contents of an existing text file.
- append_file(path, content): add text to the end of an existing file.
- replace_in_file(path, old, new): replace every occurrence of old text with new text in an existing file.
- list_projects(): list Tableau projects.
- list_workbooks(): list workbooks already published on this Tableau site.
- list_views(workbook_id): list views. Omit workbook_id to list every view.
- list_datasources(): list data sources already published on this Tableau site.
- get_view_data(view_id, max_rows): read up to max_rows CSV rows from a view.
- get_view_image(view_id, output_path): download a view as a PNG file.
- get_view_pdf(view_id, output_path): download a view as a PDF file.
- refresh_workbook(workbook_id): reload the extract for an existing workbook. List workbooks first and use an id from that result.
- refresh_datasource(datasource_id): reload the extract for an existing data source. List data sources first and use an id from that result.
- get_job(job_id): check the refresh job returned by a refresh tool.
- publish_sales_workbook(file_path, name): read the user's CSV through the file tools and publish those rows with a bar chart. name is optional.

A refresh reloads an existing extract. It does not add the user's rows.
When the user wants their own data in Tableau, call read_file on the file path they gave, then call publish_sales_workbook with that same file_path. The publish tool reads the file through the file tools and builds the extract and chart from those rows. Do not invent rows.
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
and copy ids, names, and job status from the tool result. Do not invent them.

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
                        "If this result is the user's file and they want it in Tableau, call "
                        "publish_sales_workbook with that file path. Do not paste rows into the tool. "
                        "When the requested Tableau work is done, answer in valid JSON "
                        "with data_needed=false, data=null, reprompt=false, "
                        "and response copying names, ids, row counts, and job status from the tool result."
                    )
            except (json.JSONDecodeError, KeyError) as error:
                print(f"Invalid response: {error}")
    finally:
        sign_out(session)
        print("Signed out of Tableau.")


if __name__ == "__main__":
    run()
