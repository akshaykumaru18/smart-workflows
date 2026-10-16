import time
import uuid

_DEFAULT_SCOPES = [
    "tableau:content:read",
    "tableau:tasks:run",
    "tableau:views:download",
    "tableau:workbooks:create",
]


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


def list_views(session: dict, workbook_id: str | None = None, max_items: int = 100) -> list:
    """Return views. Pass workbook_id to list that workbook's views."""
    if workbook_id:
        items = _paged(session, f"/workbooks/{workbook_id}/views", "views", "view", max_items=max_items)
    else:
        items = _paged(session, "/views", "views", "view", max_items=max_items)
    views = []
    for item in items:
        view = _view(item)
        if workbook_id and not view.get("workbook_id"):
            view["workbook_id"] = workbook_id
        views.append(view)
    return views


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


def get_view_image(session: dict, view_id: str, output_path: str | None = None) -> dict:
    """Download a rendered view as PNG. output_path defaults to reports/{view_id}.png."""
    path = output_path or f"reports/{view_id}.png"
    return _download_view(
        session,
        view_id,
        "image",
        path,
        {"resolution": "high", "vizWidth": 1200, "vizHeight": 800},
        (b"\x89PNG", b"\xff\xd8\xff"),
    )


def get_view_pdf(session: dict, view_id: str, output_path: str | None = None) -> dict:
    """Download a rendered view as PDF. output_path defaults to reports/{view_id}.pdf."""
    path = output_path or f"reports/{view_id}.pdf"
    return _download_view(
        session,
        view_id,
        "pdf",
        path,
        {"type": "A4", "orientation": "Landscape"},
        (b"%PDF",),
    )


def publish_sales_workbook(
    session: dict,
    file_path: str,
    name: str | None = None,
    chart: str | None = None,
    dimension: str | None = None,
    measure: str | None = None,
) -> dict:
    """Publish a CSV. Sheets are built from that file's columns unless dimension and measure are passed."""
    from pathlib import Path

    columns, rows, kinds = _load_user_csv(file_path)
    if dimension or measure:
        views = [_view_spec(columns, kinds, chart, dimension, measure)]
        dashboard = None
    else:
        columns, kinds, views = _views_from_data(columns, rows, kinds)
        dashboard = _dashboard_name(views)
    workbook_name = (name or Path(file_path).stem).strip() or Path(file_path).stem
    print(
        f"Publishing {workbook_name} from {file_path}: "
        + ", ".join(view["sheet"] for view in views)
    )
    projects = list_projects(session)
    if not projects:
        raise RuntimeError("Tableau site has no project to publish into")
    project = projects[0]
    slug = _file_slug(workbook_name)
    twbx_path, csv_path = _write_twbx(
        workbook_name, columns, kinds, rows, views, Path("reports") / f"{slug}.csv"
    )
    try:
        workbook = _publish_file(
            session,
            "workbooks",
            "tableau_workbook",
            twbx_path,
            _request_xml("workbook", workbook_name, project["id"]),
            {"overwrite": "true"},
        )
    finally:
        csv_path.unlink(missing_ok=True)
        twbx_path.unlink(missing_ok=True)
    published = _workbook(workbook.get("workbook") or {})
    first = views[0]
    return {
        "source_file": file_path,
        "name": workbook_name,
        "columns": columns,
        "column_kinds": kinds,
        "sheets": views,
        "dashboard": dashboard,
        "chart": first["chart"],
        "dimension": first["dimension"],
        "measure": first["measure"],
        "sheet": first["sheet"],
        "project": project,
        "row_count": len(rows),
        "workbook": published,
        "views": published.get("views") or [],
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
    views = [_view(view) for view in _as_list((item.get("views") or {}).get("view"))]
    workbook = {
        "id": item.get("id"),
        "name": item.get("name"),
        "content_url": item.get("contentUrl"),
        "webpage_url": item.get("webpageUrl"),
        "project": project.get("name"),
    }
    if views:
        workbook["views"] = views
    return workbook


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


def _download_view(session: dict, view_id: str, kind: str, output_path: str, params: dict, signatures: tuple) -> dict:
    import time
    from pathlib import Path

    view_id = str(view_id).strip()
    last_error = None
    for accept in ("*/*", "application/json"):
        for attempt in range(2):
            try:
                response = _send(
                    session,
                    "GET",
                    f"/views/{view_id}/{kind}",
                    accept=accept,
                    params=params,
                    timeout=90,
                )
            except RuntimeError as error:
                last_error = error
                message = str(error)
                if attempt == 0 and any(code in message for code in ("404", "409", "503")):
                    time.sleep(2)
                    continue
                if "406" in message:
                    break
                raise
            content = response.content
            if content.startswith(signatures):
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                return {"view_id": view_id, "file_path": str(path), "bytes_written": len(content)}
            detail = content[:500].decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Tableau {kind} did not return a file: {detail}")
    if last_error:
        raise last_error
    raise RuntimeError(f"Tableau {kind} download failed for view {view_id}")


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


_CHARTS = {
    "bar": "bar",
    "bar chart": "bar",
    "column": "bar",
    "horizontal bar": "horizontal_bar",
    "horizontal_bar": "horizontal_bar",
    "line": "line",
    "line chart": "line",
    "area": "area",
    "area chart": "area",
    "circle": "circle",
    "scatter": "circle",
    "square": "square",
    "pie": "pie",
    "pie chart": "pie",
    "table": "table",
    "text": "table",
    "none": "table",
    "data": "table",
}


def _view_spec(columns: list, kinds: dict, chart: str | None, dimension: str | None, measure: str | None) -> dict:
    requested = (chart or "table").strip().lower()
    kind = _CHARTS.get(requested)
    if kind is None:
        choices = "table, bar, horizontal_bar, line, area, circle, square, pie"
        raise ValueError(f"Unknown chart '{chart}'. Use one of: {choices}")
    text = [name for name in columns if kinds[name] == "text"]
    numbers = [name for name in columns if kinds[name] == "number"]
    if kind == "table" and not dimension and not measure:
        dimension_name = text[0] if text else None
        measure_name = numbers[0] if numbers else None
    else:
        dimension_name = _match_column(dimension, columns)
        measure_name = _match_column(measure, columns)
    if dimension_name is None or measure_name is None:
        raise ValueError(
            "Choose a text dimension and a numeric measure from the file. "
            f"Text columns: {', '.join(text) or 'none'}. "
            f"Numeric columns: {', '.join(numbers) or 'none'}."
        )
    if kinds[dimension_name] not in ("text", "date"):
        raise ValueError(f"{dimension_name} is numeric. Use a text or date column as the dimension.")
    if kinds[measure_name] != "number":
        raise ValueError(f"{measure_name} is not numeric. Use a numeric column as the measure.")
    return {
        "chart": kind,
        "dimension": dimension_name,
        "measure": measure_name,
        "sheet": f"{measure_name} by {dimension_name}",
    }


def _views_from_data(columns: list, rows: list, kinds: dict) -> tuple:
    """Build one sheet per useful category, number, and date found in this file."""
    columns = list(columns)
    kinds = dict(kinds)
    profiles = {
        name: [row[name] for row in rows if row[name] != ""]
        for name in columns
    }
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
    return columns, kinds, unique[:8]


def _sheet(chart: str, dimension: str, measure: str) -> dict:
    return {
        "chart": chart,
        "dimension": dimension,
        "measure": measure,
        "sheet": f"{measure} by {dimension}",
    }


def _dashboard_name(views: list) -> str | None:
    if len(views) < 2:
        return None
    names = {view["sheet"] for view in views}
    if "Overview" in names:
        return "Dashboard"
    return "Overview"


def _match_column(name: str | None, columns: list) -> str | None:
    if name is None or not str(name).strip():
        return None
    wanted = str(name).strip().lower()
    for column in columns:
        if column.lower() == wanted:
            return column
    raise ValueError(f"Column not in the file: {name}. Columns: {', '.join(columns)}")


def _file_slug(name: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_")
    return slug or "workbook"


def _write_csv(columns: list, rows: list, csv_path):
    import csv
    from pathlib import Path

    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_twbx(name: str, columns: list, kinds: dict, rows: list, views: list, csv_path):
    import zipfile
    from pathlib import Path

    csv_file = _write_csv(columns, rows, csv_path)
    csv_name = Path(csv_file).name
    twbx_path = Path(csv_file).with_suffix(".twbx")
    twb = _workbook_twb(name, columns, kinds, views, csv_name)
    with zipfile.ZipFile(twbx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(Path(csv_file).with_suffix(".twb").name, twb)
        archive.write(csv_file, f"Data/{csv_name}")
    return twbx_path, csv_file


def _workbook_twb(name: str, columns: list, kinds: dict, views: list, csv_name: str) -> str:
    caption = _xml_text(name)
    locals_by_column = _local_names(columns)
    column_xml = "\n".join(_column_xml(column, kinds[column]) for column in columns)
    relation_columns = "\n".join(
        _relation_column(column, kinds[column], index) for index, column in enumerate(columns)
    )
    table_name = _xml_text("[" + csv_name.replace(".", "#") + "]")
    csv_xml = _xml_text(csv_name)
    worksheets = "\n".join(
        _worksheet_xml(caption, view, kinds, locals_by_column) for view in views
    )
    dashboard = _dashboards_xml(views)
    windows = _windows_xml(views)
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.2.0' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>
    <datasource caption='{caption}' inline='true' name='federated.0' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='{caption}' name='textscan.0'>
            <connection class='textscan' directory='Data' filename='{csv_xml}' password='' server='' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.0' name='{csv_xml}' table='{table_name}' type='table'>
          <columns header='yes' outcome='6'>
{relation_columns}
          </columns>
        </relation>
      </connection>
{column_xml}
    </datasource>
  </datasources>
  <worksheets>
{worksheets}
  </worksheets>
{dashboard}  <windows source-height='30'>
{windows}
  </windows>
</workbook>
"""


_WINDOW_CARDS = """      <cards>
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
      </cards>"""


def _worksheet_xml(caption: str, view: dict, kinds: dict, locals_by_column: dict) -> str:
    dimension = view["dimension"]
    measure = view["measure"]
    sheet = _xml_text(view["sheet"])
    dimension_local = locals_by_column[dimension]
    measure_local = locals_by_column[measure]
    dimension_field = f"[federated.0].[none:{dimension_local}:nk]"
    measure_field = f"[federated.0].[sum:{measure_local}:qk]"
    mark, rows, cols, encodings = _mark_layout(view["chart"], dimension_field, measure_field)
    dimension_ref = _xml_text(_bracket(dimension))
    measure_ref = _xml_text(_bracket(measure))
    chart_xml = "\n".join(
        [
            _column_xml(dimension, kinds[dimension]),
            _column_xml(measure, kinds[measure]),
            f"            <column-instance column='{dimension_ref}' derivation='None' name='[none:{dimension_local}:nk]' pivot='key' type='nominal' />",
            f"            <column-instance column='{measure_ref}' derivation='Sum' name='[sum:{measure_local}:qk]' pivot='key' type='quantitative' />",
        ]
    )
    rows_xml = f"<rows>{rows}</rows>" if rows else "<rows />"
    cols_xml = f"<cols>{cols}</cols>" if cols else "<cols />"
    encoding_xml = f"\n            {encodings}" if encodings else ""
    return f"""    <worksheet name='{sheet}'>
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
            <mark class='{mark}' />{encoding_xml}
          </pane>
        </panes>
        {rows_xml}
        {cols_xml}
      </table>
    </worksheet>"""


def _dashboards_xml(views: list) -> str:
    title = _dashboard_name(views)
    if title is None:
        return ""
    count = len(views)
    columns = 2 if count > 1 else 1
    grid_rows = (count + columns - 1) // columns
    cell_w = 100000 // columns
    cell_h = 100000 // grid_rows
    zones = []
    for index, view in enumerate(views):
        sheet = _xml_text(view["sheet"])
        x = (index % columns) * cell_w
        y = (index // columns) * cell_h
        zones.append(
            f"          <zone h='{cell_h}' id='{index + 10}' name='{sheet}' w='{cell_w}' x='{x}' y='{y}' />"
        )
    zone_xml = "\n".join(zones)
    safe_title = _xml_text(title)
    return f"""  <dashboards>
    <dashboard name='{safe_title}'>
      <style />
      <size maxheight='800' maxwidth='1200' minheight='800' minwidth='1200' />
      <zones>
        <zone h='100000' id='2' type='layout-basic' w='100000' x='0' y='0'>
{zone_xml}
        </zone>
      </zones>
    </dashboard>
  </dashboards>
"""


def _windows_xml(views: list) -> str:
    title = _dashboard_name(views)
    windows = []
    if title is not None:
        viewpoints = "\n".join(
            f"      <viewpoint name='{_xml_text(view['sheet'])}' />" for view in views
        )
        windows.append(
            f"""    <window class='dashboard' maximized='true' name='{_xml_text(title)}'>
      <viewpoints>
{viewpoints}
      </viewpoints>
      <active id='-1' />
    </window>"""
        )
    for index, view in enumerate(views):
        maximized = " maximized='true'" if title is None and index == 0 else ""
        windows.append(
            f"""    <window class='worksheet'{maximized} name='{_xml_text(view['sheet'])}'>
{_WINDOW_CARDS}
    </window>"""
        )
    return "\n".join(windows)


def _mark_layout(chart: str, dimension_field: str, measure_field: str) -> tuple:
    if chart == "horizontal_bar":
        return "Bar", dimension_field, measure_field, ""
    if chart == "table":
        encodings = f"<encodings><text column='{measure_field}' /></encodings>"
        return "Text", dimension_field, "", encodings
    if chart == "pie":
        encodings = (
            "<encodings>"
            f"<color column='{dimension_field}' />"
            f"<wedge-size column='{measure_field}' />"
            "</encodings>"
        )
        return "Pie", "", "", encodings
    marks = {"bar": "Bar", "line": "Line", "area": "Area", "circle": "Circle", "square": "Square"}
    return marks[chart], measure_field, dimension_field, ""


def _relation_column(name: str, kind: str, index: int) -> str:
    datatype = "real" if kind == "number" else "string"
    return f"            <column datatype='{datatype}' name='{_xml_text(name)}' ordinal='{index}' />"


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


def _local_names(columns: list) -> dict:
    used = set()
    names = {}
    for column in columns:
        base = _local_name(column)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        names[column] = name
    return names


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
