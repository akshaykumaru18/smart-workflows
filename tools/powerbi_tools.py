import base64
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


def create_push_dataset(access_token: str, workspace_id: str, name: str, rows: list) -> dict:
    """Create a push dataset in a workspace and load its rows."""
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("rows must be a list of objects")

    columns = [{"name": key, "dataType": _column_type(value)} for key, value in rows[0].items()]
    created = _request(
        access_token,
        "POST",
        f"/groups/{workspace_id}/datasets",
        {
            "name": name,
            "defaultMode": "Push",
            "tables": [{"name": "ReportData", "columns": columns}],
        },
    )
    dataset_id = created["id"]
    table = quote("ReportData", safe="")
    _request(
        access_token,
        "POST",
        f"/groups/{workspace_id}/datasets/{dataset_id}/tables/{table}/rows",
        {"rows": rows},
    )
    return {
        "dataset_id": dataset_id,
        "dataset_name": name,
        "workspace_id": workspace_id,
        "table": "ReportData",
        "columns": [column["name"] for column in columns],
        "row_count": len(rows),
        "web_url": f"https://app.powerbi.com/groups/{workspace_id}/datasets/{dataset_id}/details",
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
) -> str:
    """Write a .pbix report bound to an existing Power BI dataset."""
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
        "sections": [
            {
                "id": 0,
                "name": "ReportSection",
                "displayName": "Page 1",
                "filters": "[]",
                "ordinal": 0,
                "visualContainers": [
                    {
                        "x": 20,
                        "y": 20,
                        "z": 0,
                        "width": 900,
                        "height": 480,
                        "config": _visual_config(columns),
                        "filters": "[]",
                    }
                ],
                "config": "{}",
                "displayOption": 1,
                "width": 1280,
                "height": 720,
            }
        ],
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
