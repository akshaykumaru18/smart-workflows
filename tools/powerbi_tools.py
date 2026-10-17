import base64
import csv
import io
import json
import ssl
import time
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

_API = "https://api.powerbi.com/v1.0/myorg"
_FABRIC = "https://api.fabric.microsoft.com/v1"
_TOKEN_CACHE = Path(__file__).resolve().parent.parent / ".powerbi_token_cache.json"
_TABLE = "ReportData"
_ROW_BATCH = 10000


def _ssl_context():
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _request(access_token: str, method: str, path: str, body: dict | None = None):
    data = None
    headers = {"Authorization": f"Bearer {access_token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=60, context=_ssl_context()) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Power BI request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Power BI request failed: {exc.reason}") from exc


def _load_token_cache():
    import msal

    cache = msal.SerializableTokenCache()
    if _TOKEN_CACHE.exists():
        try:
            cache.deserialize(_TOKEN_CACHE.read_text())
        except (json.JSONDecodeError, ValueError):
            return msal.SerializableTokenCache()
    return cache


def _save_token_cache(cache) -> None:
    if not cache.has_state_changed:
        return
    _TOKEN_CACHE.write_text(cache.serialize())
    _TOKEN_CACHE.chmod(0o600)


def get_device_access_token(tenant_id: str, client_id: str, scopes: list | None = None) -> str:
    """Sign in with a device code and return a Power BI access token.

    A successful sign-in is stored in .powerbi_token_cache.json. Later runs
    refresh the access token from that file until Microsoft rejects it.
    """
    import msal

    if scopes is None:
        scopes = ["https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All"]
    cache = _load_token_cache()
    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start sign-in: {flow}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
        if not result or "access_token" not in result:
            detail = result.get("error_description", "Authentication failed") if result else "Authentication failed"
            raise RuntimeError(detail)
    else:
        print("Using saved Power BI sign-in.")
    _save_token_cache(cache)
    return result["access_token"]


def get_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Return a Power BI access token for a service principal."""
    payload = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
        }
    ).encode("utf-8")
    request = Request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30, context=_ssl_context()) as response:
            token = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Power BI login failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Power BI login failed: {exc.reason}") from exc
    return token["access_token"]


def list_workspaces(access_token: str) -> list:
    """Return the workspaces this token can access."""
    return _request(access_token, "GET", "/groups")["value"]


def create_workspace(access_token: str, name: str) -> dict:
    """Create a Power BI workspace and return it."""
    return _request(access_token, "POST", "/groups", {"name": name})


def get_workspace(access_token: str, workspace_id: str) -> dict:
    """Return one workspace."""
    return _request(access_token, "GET", f"/groups/{workspace_id}")


def list_datasets(access_token: str, workspace_id: str) -> list:
    """Return datasets in a workspace."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/datasets")["value"]


def get_dataset(access_token: str, workspace_id: str, dataset_id: str) -> dict:
    """Return one dataset."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/datasets/{dataset_id}")


def refresh_dataset(access_token: str, workspace_id: str, dataset_id: str) -> None:
    """Start a dataset refresh."""
    _request(access_token, "POST", f"/groups/{workspace_id}/datasets/{dataset_id}/refreshes")


def list_reports(access_token: str, workspace_id: str) -> list:
    """Return reports in a workspace."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/reports")["value"]


def get_report(access_token: str, workspace_id: str, report_id: str) -> dict:
    """Return report metadata: name, id, semantic-model id, and URL."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/reports/{report_id}")


def get_report_pages(access_token: str, workspace_id: str, report_id: str) -> dict:
    """Return each report page and its visuals when that API is available."""
    pages = _request(
        access_token,
        "GET",
        f"/groups/{workspace_id}/reports/{report_id}/pages",
    )["value"]
    layout = []
    for page in pages:
        page_name = quote(page.get("name", ""), safe="")
        visuals = None
        try:
            visuals = _request(
                access_token,
                "GET",
                f"/groups/{workspace_id}/reports/{report_id}/pages/{page_name}/visuals",
            )["value"]
        except RuntimeError as exc:
            if not any(code in str(exc) for code in ("(400)", "(403)", "(404)", "(501)")):
                raise
        layout.append({**page, "visuals": visuals})
    return {"report_id": report_id, "pages": layout}


def export_report(
    access_token: str,
    workspace_id: str,
    report_id: str,
    path: str | None = None,
) -> dict:
    """Save report metadata and page details as a readable JSON file."""
    report = get_report(access_token, workspace_id, report_id)
    pages = get_report_pages(access_token, workspace_id, report_id)
    document = {
        "report": report,
        "pages": pages["pages"],
    }
    folder = Path("reports")
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(
        char if char.isalnum() else "_" for char in (report.get("name") or report_id)
    ).strip("_") or report_id
    destination = Path(path) if path else folder / f"{safe_name}.json"
    destination.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return {
        "report_id": report_id,
        "report_name": report.get("name"),
        "dataset_id": report.get("datasetId"),
        "page_count": len(pages["pages"]),
        "path": str(destination),
    }


def get_report_definition(access_token: str, workspace_id: str, report_id: str) -> dict:
    """Download a report definition from the Fabric API. Returns the definition payload."""
    started = _fabric_call(
        "POST",
        f"{_FABRIC}/workspaces/{workspace_id}/reports/{report_id}/getDefinition",
        access_token,
    )
    if started["status"] != 202:
        return json.loads(started["body"].decode("utf-8") or "{}")
    return _wait_for_definition(access_token, started)


def save_report_definition(definition_response: dict, output_dir: str) -> dict:
    """Write definition parts to a folder. Each part path is kept under that folder."""
    definition = definition_response.get("definition", definition_response)
    parts = definition.get("parts", [])
    if not parts:
        raise RuntimeError("No definition parts returned. Check the API response and report permissions.")

    folder = Path(output_dir)
    saved = []
    for part in parts:
        relative_path = Path(part["path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Unexpected definition part path: {part['path']}")
        destination = folder / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(part["payload"]))
        saved.append(str(destination))
    return {"output_dir": str(folder), "part_count": len(saved), "files": saved}


def extract_report_definition(
    access_token: str,
    workspace_id: str,
    report_id: str,
    output_dir: str | None = None,
) -> dict:
    """Save a report's Fabric definition parts into a folder you choose."""
    folder = output_dir or str(Path("reports") / f"{report_id}-definition")
    saved = save_report_definition(
        get_report_definition(access_token, workspace_id, report_id),
        folder,
    )
    return {"report_id": report_id, "workspace_id": workspace_id, **saved}


def _fabric_call(method: str, url: str, access_token: str) -> dict:
    data = b"{}" if method == "POST" else None
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=60, context=_ssl_context()) as response:
            return {
                "status": response.status,
                "headers": {key: value for key, value in response.headers.items()},
                "body": response.read(),
            }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fabric request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Fabric request failed: {exc.reason}") from exc


def _wait_for_definition(access_token: str, started: dict) -> dict:
    operation_url = started["headers"].get("Location") or started["headers"].get("location")
    if not operation_url:
        raise RuntimeError("Definition retrieval is running, but no operation URL was returned.")
    retry_after = _retry_after(started["headers"])
    for _ in range(40):
        time.sleep(retry_after)
        status = _fabric_call("GET", operation_url, access_token)
        result = json.loads(status["body"].decode("utf-8") or "{}")
        state = result.get("status")
        if state == "Succeeded":
            if "definition" in result:
                return result
            final = _fabric_call("GET", operation_url.rstrip("/") + "/result", access_token)
            return json.loads(final["body"].decode("utf-8") or "{}")
        if state in ("Failed", "Cancelled"):
            raise RuntimeError(f"Definition operation {state}: {result}")
        retry_after = _retry_after(status["headers"])
    raise TimeoutError("Definition retrieval timed out.")


def _retry_after(headers: dict) -> int:
    raw = headers.get("Retry-After") or headers.get("retry-after") or "5"
    try:
        return max(int(raw), 1)
    except ValueError:
        return 5


def list_dashboards(access_token: str, workspace_id: str) -> list:
    """Return dashboards in a workspace."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/dashboards")["value"]


def get_dashboard(access_token: str, workspace_id: str, dashboard_id: str) -> dict:
    """Return one dashboard."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/dashboards/{dashboard_id}")


def _column_type(value) -> str:
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Int64"
    if isinstance(value, float):
        return "Double"
    return "String"


def create_push_dataset(
    access_token: str,
    workspace_id: str,
    name: str,
    rows: list,
    columns: list | None = None,
    measures: list | None = None,
) -> dict:
    """Create a push dataset, load its rows, and add DAX measures when provided.

    columns is a list of {"name", "dataType"}. dataType is String, Int64, Double,
    Boolean, or DateTime. measures is a list of {"name", "expression"}.
    """
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("rows must be a list of objects")

    if columns is None:
        columns = [{"name": key, "dataType": _column_type(value)} for key, value in rows[0].items()]
    table_body = {"name": _TABLE, "columns": columns}
    if measures:
        table_body["measures"] = measures
    created = _request(
        access_token,
        "POST",
        f"/groups/{workspace_id}/datasets",
        {
            "name": name,
            "defaultMode": "Push",
            "tables": [table_body],
        },
    )
    dataset_id = created["id"]
    _post_rows(access_token, workspace_id, dataset_id, rows)
    return {
        "dataset_id": dataset_id,
        "dataset_name": name,
        "workspace_id": workspace_id,
        "table": _TABLE,
        "columns": [column["name"] for column in columns],
        "measures": [measure["name"] for measure in measures or []],
        "row_count": len(rows),
        "web_url": f"https://app.powerbi.com/groups/{workspace_id}/datasets/{dataset_id}/details",
    }


def _post_rows(access_token: str, workspace_id: str, dataset_id: str, rows: list) -> None:
    table = quote(_TABLE, safe="")
    path = f"/groups/{workspace_id}/datasets/{dataset_id}/tables/{table}/rows"
    for start in range(0, len(rows), _ROW_BATCH):
        _request(access_token, "POST", path, {"rows": rows[start:start + _ROW_BATCH]})


def execute_dax(access_token: str, workspace_id: str, dataset_id: str, query: str) -> dict:
    """Run a DAX query against a dataset.

    The workspace must be on a Premium, PPU, or Fabric capacity.
    Example: EVALUATE ROW("Row Count", [Row Count])
    """
    text = str(query or "").strip()
    if not text:
        raise ValueError("query is required")
    return _request(
        access_token,
        "POST",
        f"/groups/{workspace_id}/datasets/{dataset_id}/executeQueries",
        {
            "queries": [{"query": text}],
            "serializerSettings": {"includeNulls": True},
        },
    )


def export_report_file(
    access_token: str,
    workspace_id: str,
    report_id: str,
    file_format: str = "PDF",
    output_path: str | None = None,
) -> dict:
    """Export a report to PDF, PNG, or PPTX and save the file.

    PNG of a multi-page report is saved as a zip. The workspace must be on a
    Premium, PPU, Embedded, or Fabric capacity.
    """
    fmt = (file_format or "PDF").strip().upper()
    if fmt not in {"PDF", "PNG", "PPTX"}:
        raise ValueError("file_format must be PDF, PNG, or PPTX")
    started = _request(
        access_token,
        "POST",
        f"/groups/{workspace_id}/reports/{report_id}/ExportTo",
        {"format": fmt},
    )
    export_id = started["id"]
    status = started
    for _ in range(40):
        if status.get("status") in {"Succeeded", "Failed"}:
            break
        time.sleep(3)
        status = _request(
            access_token,
            "GET",
            f"/groups/{workspace_id}/reports/{report_id}/exports/{export_id}",
        )
    if status.get("status") != "Succeeded":
        raise RuntimeError(f"Power BI export did not succeed: {json.dumps(status)}")

    request = Request(
        f"{_API}/groups/{workspace_id}/reports/{report_id}/exports/{export_id}/file",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120, context=_ssl_context()) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Power BI export download failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Power BI export download failed: {exc.reason}") from exc

    folder = Path("reports")
    folder.mkdir(parents=True, exist_ok=True)
    suffix = fmt.lower()
    if fmt == "PNG" and payload[:2] == b"PK":
        suffix = "zip"
    destination = Path(output_path) if output_path else folder / f"{report_id}.{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {
        "report_id": report_id,
        "format": fmt,
        "path": str(destination),
        "bytes": len(payload),
    }


def _sections(columns: list[str], pages: list | None) -> list:
    if not pages:
        return [_section(0, "Page 1", [_table_container(columns)])]
    sections = []
    for index, page in enumerate(pages):
        containers = [_chart_container(visual) for visual in page["visuals"]]
        sections.append(_section(index, page["title"], containers))
    return sections


def _section(index: int, title: str, containers: list) -> dict:
    return {
        "id": index,
        "name": f"ReportSection{index}",
        "displayName": title,
        "filters": "[]",
        "ordinal": index,
        "visualContainers": containers,
        "config": "{}",
        "displayOption": 1,
        "width": 1280,
        "height": 720,
    }


def _table_container(columns: list[str]) -> dict:
    return {
        "x": 20,
        "y": 20,
        "z": 0,
        "width": 900,
        "height": 480,
        "config": _visual_config(columns),
        "filters": "[]",
    }


def _chart_container(visual: dict) -> dict:
    return {
        "x": visual["x"],
        "y": visual["y"],
        "z": 0,
        "width": visual["width"],
        "height": visual["height"],
        "config": json.dumps(_chart_config(visual)),
        "filters": "[]",
    }


def _chart_config(visual: dict) -> dict:
    chart = visual["chart"]
    visual_type = {
        "horizontal_bar": "clusteredBarChart",
        "bar": "clusteredColumnChart",
        "line": "lineChart",
        "table": "tableEx",
    }.get(chart)
    if visual_type is None:
        raise ValueError(f"Unknown chart '{chart}'. Use horizontal_bar, bar, line, or table.")
    category = _column_select(visual["dimension"])
    measure = _measure_select(visual.get("measure_name") or f"Total {visual['measure']}")
    if chart == "table":
        projections = {"Values": [{"queryRef": category["Name"]}, {"queryRef": measure["Name"]}]}
    else:
        projections = {
            "Category": [{"queryRef": category["Name"]}],
            "Y": [{"queryRef": measure["Name"]}],
        }
    return {
        "name": uuid.uuid4().hex[:16],
        "layouts": [
            {
                "id": 0,
                "position": {
                    "x": visual["x"],
                    "y": visual["y"],
                    "z": 0,
                    "width": visual["width"],
                    "height": visual["height"],
                    "tabOrder": 0,
                },
            }
        ],
        "singleVisual": {
            "visualType": visual_type,
            "projections": projections,
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "d", "Entity": _TABLE, "Type": 0}],
                "Select": [category, measure],
            },
            "drillFilterOtherVisuals": True,
        },
    }


def _column_select(column: str) -> dict:
    name = f"{_TABLE}.{column}"
    return {
        "Column": {
            "Expression": {"SourceRef": {"Source": "d"}},
            "Property": column,
        },
        "Name": name,
        "NativeReferenceName": column,
    }


def _measure_select(measure_name: str) -> dict:
    name = f"{_TABLE}.{measure_name}"
    return {
        "Measure": {
            "Expression": {"SourceRef": {"Source": "d"}},
            "Property": measure_name,
        },
        "Name": name,
        "NativeReferenceName": measure_name,
    }


def _utf16(text: str) -> bytes:
    return b"\xff\xfe" + text.encode("utf-16-le")


def _visual_config(columns: list[str]) -> str:
    selected = []
    for column in columns:
        selected.append(
            {
                "Column": {
                    "Expression": {"SourceRef": {"Source": "d"}},
                    "Property": column,
                },
                "Name": f"ReportData.{column}",
                "NativeReferenceName": column,
            }
        )
    config = {
        "name": uuid.uuid4().hex[:16],
        "layouts": [
            {
                "id": 0,
                "position": {"x": 20, "y": 20, "z": 0, "width": 900, "height": 480, "tabOrder": 0},
            }
        ],
        "singleVisual": {
            "visualType": "tableEx",
            "projections": {"Values": [{"queryRef": item["Name"]} for item in selected]},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "d", "Entity": "ReportData", "Type": 0}],
                "Select": selected,
            },
            "drillFilterOtherVisuals": True,
        },
    }
    return json.dumps(config)


def create_pbix(
    dataset_id: str,
    dataset_name: str,
    workspace_name: str,
    columns: list[str],
    path: str | None = None,
    pages: list | None = None,
) -> str:
    """Write a .pbix report bound to an existing Power BI dataset.

    pages is a list of {"title", "visuals"}. Each visual has chart, dimension,
    measure, measure_name, x, y, width, and height. chart is horizontal_bar,
    bar, line, or table. Without pages, the file has one table of columns.
    """
    if not workspace_name:
        raise ValueError("workspace_name is required")
    safe_name = "".join(char if char.isalnum() else "_" for char in dataset_name).strip("_") or "report"
    folder = Path("reports")
    folder.mkdir(parents=True, exist_ok=True)
    pbix_path = Path(path) if path else folder / f"{safe_name}.pbix"

    layout = {
        "id": 0,
        "layoutOptimization": 0,
        "resourcePackages": [],
        "sections": _sections(columns, pages),
        "config": json.dumps(
            {
                "version": "5.41",
                "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": 2}},
                "activeSectionIndex": 0,
                "defaultDrillFilterOtherVisuals": True,
            }
        ),
    }
    connections = {
        "Version": 3,
        "RemoteArtifacts": [{"DatasetId": dataset_id}],
    }
    content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="json" ContentType="" />
<Override PartName="/Version" ContentType="" />
<Override PartName="/Report/Layout" ContentType="" />
<Override PartName="/Settings" ContentType="application/json" />
<Override PartName="/Metadata" ContentType="application/json" />
<Override PartName="/Connections" ContentType="" />
<Override PartName="/Report/LinguisticSchema" ContentType="" />
</Types>
"""
    files = {
        "[Content_Types].xml": content_types.encode("utf-8"),
        "Version": b"1.28",
        "Settings": b'{"Version":4,"ReportSettings":{}}',
        "Metadata": json.dumps(
            {"Version": 5, "CreatedFrom": "Cloud", "CreatedFromRelease": "2024.1"}
        ).encode("utf-8"),
        "Connections": json.dumps(connections).encode("utf-8"),
        "Report/Layout": _utf16(json.dumps(layout)),
        "Report/LinguisticSchema": _utf16('{"Entities":[]}'),
    }
    with zipfile.ZipFile(pbix_path, "w") as archive:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED if name == "Version" else zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return str(pbix_path)


def import_pbix(access_token: str, workspace_id: str, pbix_path: str, display_name: str) -> dict:
    """Upload a .pbix file into a Power BI workspace and wait until import finishes."""
    file_path = Path(pbix_path)
    boundary = f"----PowerBI{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8"),
            file_path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    query = urlencode({"datasetDisplayName": display_name, "nameConflict": "CreateOrOverwrite"})
    request = Request(
        f"{_API}/groups/{workspace_id}/imports?{query}",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120, context=_ssl_context()) as response:
            started = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Power BI import failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Power BI import failed: {exc.reason}") from exc

    import_id = started["id"]
    for _ in range(15):
        current = _request(access_token, "GET", f"/groups/{workspace_id}/imports/{import_id}")
        if current.get("importState") in {"Succeeded", "Failed"}:
            return current
        time.sleep(2)
    return current


def publish_powerbi_report(
    access_token: str,
    workspace_id: str,
    file_path: str,
    name: str | None = None,
    chart: str | None = None,
    dimension: str | None = None,
    measure: str | None = None,
) -> dict:
    """Publish a CSV as a push dataset with DAX measures and a report of charts.

    Leave chart, dimension, and measure empty so pages follow the file's columns:
    a bar for each numeric column by a category, and a line over a date column.
    """
    columns, rows, kinds = _load_csv(file_path)
    columns, rows, kinds, views = _views_for(columns, rows, kinds, chart, dimension, measure)
    schema, typed_rows = _schema_and_rows(columns, kinds, rows)
    measure_defs, measure_names = _measures_for(columns, views)
    pages = _report_pages(views, measure_names)
    report_name = name or Path(file_path).stem
    workspace = get_workspace(access_token, workspace_id)
    dataset = create_push_dataset(
        access_token,
        workspace_id,
        report_name,
        typed_rows,
        columns=schema,
        measures=measure_defs,
    )
    pbix_path = create_pbix(
        dataset["dataset_id"],
        report_name,
        workspace.get("name") or workspace_id,
        columns,
        pages=pages,
    )
    imported = import_pbix(access_token, workspace_id, pbix_path, report_name)
    if imported.get("importState") != "Succeeded":
        raise RuntimeError(f"Power BI import did not succeed: {json.dumps(imported)}")
    report = (imported.get("reports") or [{}])[0]
    report_id = report.get("id")
    return {
        "name": report_name,
        "file_path": file_path,
        "row_count": len(typed_rows),
        "workspace_id": workspace_id,
        "dataset_id": dataset["dataset_id"],
        "dataset_url": dataset["web_url"],
        "report_id": report_id,
        "report_url": report.get("webUrl")
        or (
            f"https://app.powerbi.com/groups/{workspace_id}/reports/{report_id}"
            if report_id
            else None
        ),
        "pages": [page["title"] for page in pages],
        "measures": measure_defs,
        "pbix_path": pbix_path,
    }


def _load_csv(file_path: str) -> tuple:
    from tools.file_tools import read_file

    text = read_file(file_path)
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"CSV has no header row: {file_path}")

    columns = []
    sources = []
    for original in reader.fieldnames:
        if original is None or not str(original).strip():
            continue
        header = str(original).strip()
        if header in columns:
            raise ValueError(f"Duplicate column: {header}")
        columns.append(header)
        sources.append(original)
    if not columns:
        raise ValueError(f"CSV has no columns: {file_path}")

    rows = []
    for raw in reader:
        row = {header: (raw.get(source) or "").strip() for header, source in zip(columns, sources)}
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ValueError(f"CSV has no data rows: {file_path}")
    return columns, rows, _column_kinds(columns, rows)


def _column_kinds(columns: list, rows: list) -> dict:
    kinds = {}
    for name in columns:
        values = [row[name] for row in rows if row[name] != ""]
        if values and all(_looks_like_date(value) for value in values):
            kinds[name] = "date"
        elif values and all(_is_number(value) for value in values):
            kinds[name] = "number"
        else:
            kinds[name] = "text"
    return kinds


def _looks_like_date(value: str) -> bool:
    if len(value) < 10 or value[4] != "-" or value[7] != "-":
        return False
    if len(value) > 10 and value[10] not in " T":
        return False
    try:
        year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    except ValueError:
        return False
    return year >= 1000 and 1 <= month <= 12 and 1 <= day <= 31


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _is_identifier(name: str, rows: list) -> bool:
    folded = "".join(ch.lower() if ch.isalnum() else " " for ch in name)
    tokens = [token for token in folded.split() if token]
    if "id" not in tokens and not folded.replace(" ", "").endswith("id"):
        return False
    if folded.replace(" ", "").endswith("valid"):
        return False
    values = [row[name] for row in rows if row[name] != ""]
    if not values:
        return False
    return len(set(values)) >= len(values) * 0.9


def _is_category(distinct: int, nonempty: int) -> bool:
    if distinct < 2 or distinct > 30 or nonempty == 0:
        return False
    return (distinct / nonempty) <= 0.5 or distinct <= 8


def _views_for(columns: list, rows: list, kinds: dict, chart, dimension, measure) -> tuple:
    if chart or dimension or measure:
        return columns, rows, kinds, [_one_view(columns, kinds, chart, dimension, measure)]
    return _views_from_data(columns, rows, kinds)


def _one_view(columns: list, kinds: dict, chart, dimension, measure) -> dict:
    requested = (chart or "table").strip().lower()
    charts = {
        "bar": "bar",
        "bar chart": "bar",
        "column": "bar",
        "horizontal bar": "horizontal_bar",
        "horizontal_bar": "horizontal_bar",
        "line": "line",
        "line chart": "line",
        "table": "table",
        "text": "table",
    }
    kind = charts.get(requested)
    if kind is None:
        raise ValueError("Unknown chart. Use table, bar, horizontal_bar, or line.")
    text = [name for name in columns if kinds[name] in {"text", "date"}]
    numbers = [name for name in columns if kinds[name] == "number"]
    dimension_name = _match_column(dimension, columns) or (text[0] if text else None)
    measure_name = _match_column(measure, columns) or (numbers[0] if numbers else None)
    if dimension_name is None or measure_name is None:
        raise ValueError(
            "Choose a text or date dimension and a numeric measure from the file. "
            f"Columns: {', '.join(columns)}."
        )
    if kinds[dimension_name] not in {"text", "date"}:
        raise ValueError(f"{dimension_name} is numeric. Use a text or date column as the dimension.")
    if kinds[measure_name] != "number":
        raise ValueError(f"{measure_name} is not numeric. Use a numeric column as the measure.")
    return _sheet(kind, dimension_name, measure_name)


def _views_from_data(columns: list, rows: list, kinds: dict) -> tuple:
    columns = list(columns)
    kinds = dict(kinds)
    profiles = {name: [row[name] for row in rows if row[name] != ""] for name in columns}
    measures = [
        name
        for name in columns
        if kinds[name] == "number" and not _is_identifier(name, rows)
    ][:4]
    categories = [
        name
        for name in columns
        if kinds[name] == "text"
        and not _is_identifier(name, rows)
        and _is_category(len(set(profiles[name])), len(profiles[name]))
    ]
    categories.sort(key=lambda name: (len(set(profiles[name])), columns.index(name)))
    categories = categories[:4]
    dates = [name for name in columns if kinds[name] == "date" and not _is_identifier(name, rows)]
    if not measures and (categories or dates):
        measure_name = "Row Count" if "Records" in columns else "Records"
        columns.append(measure_name)
        kinds[measure_name] = "number"
        for row in rows:
            row[measure_name] = "1"
        measures = [measure_name]
    views = []
    if categories and measures:
        for measure in measures:
            views.append(_sheet("horizontal_bar", categories[0], measure))
    if dates and measures:
        for measure in measures:
            views.append(_sheet("line", dates[0], measure))
    if categories and measures:
        for category in categories[1:]:
            views.append(_sheet("horizontal_bar", category, measures[0]))
    if not views:
        labels = ", ".join(f"{name} ({kinds[name]})" for name in columns)
        raise ValueError(
            "This file has no category or date column to graph against its numbers. "
            f"Columns: {labels}."
        )
    unique = []
    seen = set()
    for view in views:
        if view["sheet"] in seen:
            continue
        seen.add(view["sheet"])
        unique.append(view)
    return columns, rows, kinds, unique[:8]


def _sheet(chart: str, dimension: str, measure: str) -> dict:
    return {
        "chart": chart,
        "dimension": dimension,
        "measure": measure,
        "sheet": f"{measure} by {dimension}",
    }


def _match_column(name: str | None, columns: list) -> str | None:
    if name is None or not str(name).strip():
        return None
    wanted = str(name).strip().lower()
    for column in columns:
        if column.lower() == wanted:
            return column
    raise ValueError(f"Column not in the file: {name}. Columns: {', '.join(columns)}")


def _schema_and_rows(columns: list, kinds: dict, rows: list) -> tuple:
    number_types = {}
    schema = []
    for name in columns:
        if kinds[name] == "number":
            values = [row[name] for row in rows if row[name] != ""]
            number_types[name] = _number_type(values)
            schema.append({"name": name, "dataType": number_types[name]})
        elif kinds[name] == "date":
            schema.append({"name": name, "dataType": "DateTime"})
        else:
            schema.append({"name": name, "dataType": "String"})
    typed_rows = []
    for row in rows:
        typed = {}
        for name in columns:
            raw = row[name]
            if raw == "":
                typed[name] = None
            elif kinds[name] == "number":
                number = float(raw)
                typed[name] = int(number) if number_types[name] == "Int64" else number
            elif kinds[name] == "date":
                typed[name] = raw if "T" in raw else f"{raw[:10]}T00:00:00"
            else:
                typed[name] = raw
        typed_rows.append(typed)
    return schema, typed_rows


def _number_type(values: list) -> str:
    for value in values:
        if not float(value).is_integer():
            return "Double"
    return "Int64"


def _measures_for(columns: list, views: list) -> tuple:
    row_name = "Row Count" if "Row Count" not in columns else "Report Row Count"
    measures = [
        {
            "name": row_name,
            "expression": f"COUNTROWS({_dax_table(_TABLE)})",
        }
    ]
    mapping = {}
    for view in views:
        column = view["measure"]
        if column in mapping:
            continue
        measure_name = f"Total {column}"
        if measure_name in columns:
            measure_name = f"{measure_name} Measure"
        mapping[column] = measure_name
        measures.append(
            {
                "name": measure_name,
                "expression": f"SUM({_dax_column(_TABLE, column)})",
            }
        )
    return measures, mapping


def _dax_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _dax_column(table: str, column: str) -> str:
    return f"{_dax_table(table)}[{column.replace(']', ']]')}]"


def _report_pages(views: list, measure_names: dict) -> list:
    pages = []
    for view in views:
        pages.append(
            {
                "title": view["sheet"],
                "visuals": [_placed(view, measure_names, 20, 20, 900, 480)],
            }
        )
    if len(views) < 2:
        return pages
    titles = {view["sheet"] for view in views}
    overview = "Dashboard" if "Overview" in titles else "Overview"
    visuals = []
    for index, view in enumerate(views[:4]):
        column = index % 2
        row = index // 2
        visuals.append(
            _placed(view, measure_names, 20 + column * 620, 20 + row * 340, 600, 320)
        )
    pages.append({"title": overview, "visuals": visuals})
    return pages


def _placed(view: dict, measure_names: dict, x: int, y: int, width: int, height: int) -> dict:
    return {
        "chart": view["chart"],
        "dimension": view["dimension"],
        "measure": view["measure"],
        "measure_name": measure_names[view["measure"]],
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
