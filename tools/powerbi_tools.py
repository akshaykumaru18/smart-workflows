import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_API = "https://api.powerbi.com/v1.0/myorg"


def _request(access_token: str, method: str, path: str):
    request = Request(
        f"{_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        method=method,
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Power BI request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Power BI request failed: {exc.reason}") from exc


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
        with urlopen(request, timeout=30) as response:
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
    """Return one report."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/reports/{report_id}")


def list_dashboards(access_token: str, workspace_id: str) -> list:
    """Return dashboards in a workspace."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/dashboards")["value"]


def get_dashboard(access_token: str, workspace_id: str, dashboard_id: str) -> dict:
    """Return one dashboard."""
    return _request(access_token, "GET", f"/groups/{workspace_id}/dashboards/{dashboard_id}")
