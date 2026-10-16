import time
import uuid

_DEFAULT_SCOPES = ["tableau:content:read", "tableau:tasks:run"]


def generate_jwt(
    client_id: str,
    secret_id: str,
    secret_value: str,
    username: str,
    scopes: list | None = None,
) -> str:
    """Build a Tableau connected-app JWT for the given user."""
    import jwt

    now = int(time.time())
    claims = {
        "iss": client_id,
        "sub": username,
        "aud": "tableau",
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "scp": list(scopes or _DEFAULT_SCOPES),
    }
    headers = {
        "kid": secret_id,
        "iss": client_id,
        "alg": "HS256",
    }
    token = jwt.encode(claims, secret_value, algorithm="HS256", headers=headers)
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


def sign_in(
    server: str,
    site_content_url: str,
    client_id: str,
    secret_id: str,
    secret_value: str,
    username: str,
    api_version: str = "3.29",
    scopes: list | None = None,
) -> dict:
    """Sign in to Tableau with a connected-app JWT and return the session."""
    import requests

    server = server.rstrip("/")
    response = requests.post(
        f"{server}/api/{api_version}/auth/signin",
        json={
            "credentials": {
                "jwt": generate_jwt(client_id, secret_id, secret_value, username, scopes),
                "site": {"contentUrl": site_content_url},
            }
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    _check(response, "sign-in")
    credentials = response.json()["credentials"]
    return {
        "token": credentials["token"],
        "site_id": credentials["site"]["id"],
        "user_id": credentials["user"]["id"],
        "server": server,
        "api_version": api_version,
    }


def sign_out(session: dict) -> None:
    """End a Tableau session."""
    import requests

    response = requests.post(
        _url(session, "/auth/signout"),
        headers={"X-Tableau-Auth": session["token"]},
        timeout=30,
    )
    _check(response, "sign-out")


def list_workbooks(session: dict, max_items: int = 1000) -> list:
    """Return workbooks on the signed-in Tableau site."""
    items = _paged(session, "/workbooks", "workbooks", "workbook", max_items=max_items)
    return [_workbook(item) for item in items]


def list_views(session: dict, workbook_id: str | None = None, max_items: int = 10) -> list:
    """Return views on the site. Pass workbook_id to keep one workbook."""
    items = _paged(session, "/views", "views", "view", max_items=max_items)
    if workbook_id:
        items = [item for item in items if (item.get("workbook") or {}).get("id") == workbook_id]
    return [_view(item) for item in items]


def list_datasources(session: dict, max_items: int = 1000) -> list:
    """Return published data sources on the signed-in Tableau site."""
    items = _paged(session, "/datasources", "datasources", "datasource", max_items=max_items)
    return [_datasource(item) for item in items]


def list_projects(session: dict, max_items: int = 1000) -> list:
    """Return projects on the signed-in Tableau site."""
    items = _paged(session, "/projects", "projects", "project", max_items=max_items)
    return [{"id": item.get("id"), "name": item.get("name")} for item in items]


def get_view_data(session: dict, view_id: str, max_rows: int = 500) -> dict:
    """Return CSV rows from a Tableau view, limited to max_rows."""
    import csv
    import io

    response = _send(session, "GET", f"/views/{view_id}/data", accept="text/csv", timeout=90)
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    columns = list(reader.fieldnames or [])
    rows = []
    truncated = False
    for row in reader:
        if len(rows) >= max_rows:
            truncated = True
            break
        rows.append(dict(row))
    return {
        "view_id": view_id,
        "columns": columns,
        "rows": rows,
        "row_count_returned": len(rows),
        "truncated": truncated,
    }


def get_view_image(session: dict, view_id: str, output_path: str) -> dict:
    """Download a rendered view image and return the file path."""
    return _download_view(session, view_id, "image", "image/png", output_path)


def get_view_pdf(session: dict, view_id: str, output_path: str) -> dict:
    """Download a rendered view PDF and return the file path."""
    return _download_view(session, view_id, "pdf", "application/pdf", output_path)


def publish_sales_workbook(session: dict, file_path: str, name: str | None = None) -> dict:
    """Read a local CSV with the file tools and publish those rows as a Tableau workbook."""
    from pathlib import Path

    columns, rows, kinds = _load_user_csv(file_path)
    dimension, measure = _chart_fields(columns, kinds)
    workbook_name = (name or Path(file_path).stem).strip() or Path(file_path).stem
    projects = list_projects(session)
    if not projects:
        raise RuntimeError("Tableau site has no project to publish into")
    project = projects[0]
    slug = _file_slug(workbook_name)
    hyper_path = _write_hyper(columns, kinds, rows, Path("reports") / f"{slug}.hyper")
    twbx_path = _write_twbx(workbook_name, columns, kinds, dimension, measure, hyper_path)
    try:
        datasource = _publish_file(
            session,
            "datasources",
            "tableau_datasource",
            hyper_path,
            _request_xml("datasource", workbook_name, project["id"]),
            {"overwrite": "true", "datasourceType": "hyper"},
        )
        workbook = _publish_file(
            session,
            "workbooks",
            "tableau_workbook",
            twbx_path,
            _request_xml("workbook", workbook_name, project["id"]),
            {"overwrite": "true"},
        )
    finally:
        hyper_path.unlink(missing_ok=True)
        twbx_path.unlink(missing_ok=True)
    return {
        "source_file": file_path,
        "name": workbook_name,
        "columns": columns,
        "dimension": dimension,
        "measure": measure,
        "project": project,
        "row_count": len(rows),
        "datasource": _datasource(datasource.get("datasource") or {}),
        "workbook": _workbook(workbook.get("workbook") or {}),
    }


def refresh_workbook(session: dict, workbook_id: str) -> dict:
    """Start an extract refresh for an existing workbook. Returns the Tableau job."""
    return _refresh(session, "workbooks", workbook_id)


def refresh_datasource(session: dict, datasource_id: str) -> dict:
    """Start an extract refresh for an existing data source. Returns the Tableau job."""
    return _refresh(session, "datasources", datasource_id)


def get_job(session: dict, job_id: str) -> dict:
    """Return the status of a Tableau job."""
    import requests

    response = requests.get(
        _url(session, f"/jobs/{job_id}"),
        headers=_auth_headers(session),
        timeout=30,
    )
    _check(response, "job query")
    return _job(response.json().get("job", {}))


def _refresh(session: dict, collection: str, item_id: str) -> dict:
    import requests

    response = requests.post(
        _url(session, f"/{collection}/{item_id}/refresh"),
        headers=_auth_headers(session),
        timeout=60,
    )
    _check(response, "refresh")
    return _job(response.json().get("job", {}))




def _url(session: dict, path: str) -> str:
    site_id = session["site_id"]
    base = f"{session['server']}/api/{session['api_version']}"
    if path == "/auth/signout":
        return f"{base}{path}"
    return f"{base}/sites/{site_id}{path}"


def _auth_headers(session: dict) -> dict:
    return {
        "X-Tableau-Auth": session["token"],
        "Accept": "application/json",
    }


def _check(response, action: str) -> None:
    if not response.ok:
        raise RuntimeError(f"Tableau {action} failed: {response.status_code} {response.text}")


def _as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _workbook(item: dict) -> dict:
    project = item.get("project") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "content_url": item.get("contentUrl"),
        "webpage_url": item.get("webpageUrl"),
        "project": project.get("name"),
    }


def _datasource(item: dict) -> dict:
    project = item.get("project") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item.get("type"),
        "content_url": item.get("contentUrl"),
        "webpage_url": item.get("webpageUrl"),
        "project": project.get("name"),
    }


def _job(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "mode": item.get("mode"),
        "type": item.get("type"),
        "status": item.get("status"),
        "progress": item.get("progress"),
        "finish_code": item.get("finishCode"),
        "notes": item.get("notes"),
        "created_at": item.get("createdAt"),
        "ended_at": item.get("endedAt") or item.get("completedAt"),
    }


def _paged(session: dict, path: str, collection_key: str, item_key: str, max_items: int = 1000) -> list:
    import requests

    items = []
    page = 1
    while len(items) < max_items:
        response = requests.get(
            _url(session, path),
            headers=_auth_headers(session),
            params={"pageSize": min(100, max_items - len(items)), "pageNumber": page},
            timeout=30,
        )
        _check(response, "query")
        body = response.json()
        chunk = _as_list((body.get(collection_key) or {}).get(item_key))
        if not chunk:
            break
        items.extend(chunk)
        total = int((body.get("pagination") or {}).get("totalAvailable", len(items)))
        if len(items) >= total or len(items) >= max_items:
            break
        page += 1
    return items[:max_items]


def _send(session: dict, method: str, path: str, *, accept: str, params: dict | None = None, data: bytes | None = None, content_type: str | None = None, timeout: int = 60):
    import requests

    headers = _auth_headers(session)
    headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    response = requests.request(
        method,
        _url(session, path),
        headers=headers,
        params=params,
        data=data,
        timeout=timeout,
    )
    _check(response, path)
    return response


def _download_view(session: dict, view_id: str, kind: str, accept: str, output_path: str) -> dict:
    from pathlib import Path

    response = _send(session, "GET", f"/views/{view_id}/{kind}", accept=accept, timeout=90)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return {"view_id": view_id, "file_path": str(path), "bytes_written": len(response.content)}


def _view(item: dict) -> dict:
    workbook = item.get("workbook") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "content_url": item.get("contentUrl"),
        "workbook_id": workbook.get("id"),
        "workbook_name": workbook.get("name"),
        "view_url": item.get("viewUrlName"),
    }


def _load_user_csv(file_path: str) -> tuple:
    """Read the user's CSV through the file tools and return columns, rows, and kinds."""
    import csv
    import io

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
        name = str(original).strip()
        if name in columns:
            raise ValueError(f"Duplicate column: {name}")
        columns.append(name)
        sources.append(original)
    if not columns:
        raise ValueError(f"CSV has no columns: {file_path}")

    rows = []
    for raw in reader:
        row = {name: (raw.get(source) or "").strip() for name, source in zip(columns, sources)}
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ValueError(f"CSV has no data rows: {file_path}")
    return columns, rows, _column_kinds(columns, rows)


def _column_kinds(columns: list, rows: list) -> dict:
    kinds = {}
    for name in columns:
        numeric = True
        seen = False
        for row in rows:
            value = row[name]
            if value == "":
                continue
            seen = True
            try:
                float(value)
            except ValueError:
                numeric = False
                break
        kinds[name] = "number" if seen and numeric else "text"
    return kinds


def _chart_fields(columns: list, kinds: dict) -> tuple:
    text = [name for name in columns if kinds[name] == "text"]
    numbers = [name for name in columns if kinds[name] == "number"]
    dimension = next((name for name in ("Region", "Category", "Product") if name in text), None)
    if dimension is None and text:
        dimension = text[0]
    measure = next((name for name in ("Sales", "Profit") if name in numbers), None)
    if measure is None and numbers:
        measure = numbers[0]
    if dimension is None or measure is None:
        raise ValueError("CSV needs at least one text column and one numeric column")
    return dimension, measure


def _file_slug(name: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_")
    return slug or "workbook"


def _write_hyper(columns: list, kinds: dict, rows: list, hyper_path):
    from pathlib import Path
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        Inserter,
        SqlType,
        TableDefinition,
        TableName,
        Telemetry,
    )

    path = Path(hyper_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = TableDefinition(
        table_name=TableName("Extract", "Extract"),
        columns=[
            TableDefinition.Column(
                name,
                SqlType.double() if kinds[name] == "number" else SqlType.text(),
            )
            for name in columns
        ],
    )
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(
            endpoint=hyper.endpoint,
            database=str(path),
            create_mode=CreateMode.CREATE_AND_REPLACE,
        ) as connection:
            connection.catalog.create_schema("Extract")
            connection.catalog.create_table(table)
            with Inserter(connection, table) as inserter:
                inserter.add_rows(
                    [[_hyper_value(kinds[name], row[name]) for name in columns] for row in rows]
                )
                inserter.execute()
    return path


def _hyper_value(kind: str, value: str):
    if value == "":
        return None
    if kind == "number":
        return float(value)
    return value


def _write_twbx(name: str, columns: list, kinds: dict, dimension: str, measure: str, hyper_path):
    import zipfile
    from pathlib import Path

    hyper_name = Path(hyper_path).name
    twbx_path = Path(hyper_path).with_suffix(".twbx")
    twb = _workbook_twb(name, columns, kinds, dimension, measure, hyper_name)
    with zipfile.ZipFile(twbx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(Path(hyper_path).with_suffix(".twb").name, twb)
        archive.write(hyper_path, f"Data/{hyper_name}")
    return twbx_path


def _workbook_twb(name: str, columns: list, kinds: dict, dimension: str, measure: str, hyper_name: str) -> str:
    caption = _xml_text(name)
    sheet = _xml_text(f"{measure} by {dimension}")
    dimension_ref = _xml_text(_bracket(dimension))
    measure_ref = _xml_text(_bracket(measure))
    dimension_local = _local_name(dimension)
    measure_local = _local_name(measure)
    column_xml = "\n".join(_column_xml(column, kinds[column]) for column in columns)
    chart_xml = "\n".join(
        [
            _column_xml(dimension, kinds[dimension]),
            _column_xml(measure, kinds[measure]),
            f"            <column-instance column='{dimension_ref}' derivation='None' name='[none:{dimension_local}:nk]' pivot='key' type='nominal' />",
            f"            <column-instance column='{measure_ref}' derivation='Sum' name='[sum:{measure_local}:qk]' pivot='key' type='quantitative' />",
        ]
    )
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.2.0' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>
    <datasource caption='{caption}' inline='true' name='federated.0' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='{caption}' name='hyper.0'>
            <connection authentication='auth-none' class='hyper' dbname='Data/{_xml_text(hyper_name)}' schema='Extract' tablename='Extract' />
          </named-connection>
        </named-connections>
        <relation connection='hyper.0' name='Extract' table='[Extract].[Extract]' type='table' />
      </connection>
{column_xml}
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='{sheet}'>
      <table>
        <view>
          <datasources>
            <datasource caption='{caption}' name='federated.0' />
          </datasources>
          <datasource-dependencies datasource='federated.0'>
{chart_xml}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Bar' />
          </pane>
        </panes>
        <rows>[federated.0].[sum:{measure_local}:qk]</rows>
        <cols>[federated.0].[none:{dimension_local}:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
  <windows source-height='30'>
    <window class='worksheet' maximized='true' name='{sheet}'>
      <cards>
        <edge name='left'>
          <strip size='160'>
            <card type='pages' />
            <card type='filters' />
            <card type='marks' />
          </strip>
        </edge>
        <edge name='top'>
          <strip size='31'>
            <card type='columns' />
          </strip>
          <strip size='31'>
            <card type='rows' />
          </strip>
        </edge>
      </cards>
    </window>
  </windows>
</workbook>
"""


def _column_xml(name: str, kind: str) -> str:
    if kind == "number":
        datatype, role, semantic = "real", "measure", "quantitative"
    else:
        datatype, role, semantic = "string", "dimension", "nominal"
    return (
        f"      <column datatype='{datatype}' name='{_xml_text(_bracket(name))}' "
        f"role='{role}' type='{semantic}' />"
    )


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _local_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    return cleaned or "Field"


def _xml_text(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _request_xml(kind: str, name: str, project_id: str) -> str:
    safe_name = (
        name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        "<tsRequest>"
        f"<{kind} name=\"{safe_name}\">"
        f"<project id=\"{project_id}\"/>"
        f"</{kind}>"
        "</tsRequest>"
    )


def _publish_file(session: dict, collection: str, part_name: str, file_path, xml: str, params: dict) -> dict:
    payload = PathBytes(file_path)
    boundary = f"tableau{uuid.uuid4().hex}"
    filename = payload.name
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: name="request_payload"\r\n',
            b"Content-Type: text/xml\r\n\r\n",
            xml.encode("utf-8"),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: name="{part_name}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            payload.data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    response = _send(
        session,
        "POST",
        f"/{collection}",
        accept="application/json",
        params=params,
        data=body,
        content_type=f"multipart/mixed; boundary={boundary}",
        timeout=120,
    )
    if not response.content:
        return {}
    return response.json()


class PathBytes:
    def __init__(self, file_path):
        from pathlib import Path

        path = Path(file_path)
        self.name = path.name
        self.data = path.read_bytes()
