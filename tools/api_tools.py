import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def api_call(method: str, url: str, headers: dict | None = None, body=None, timeout: int = 30) -> dict:
    """Call an HTTP API and return status, headers, and body."""
    data = None
    request_headers = dict(headers or {})

    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif body is not None:
        raise ValueError("body must be a dict, list, string, or None")

    request = Request(url, data=data, headers=request_headers, method=method.upper())

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return {
                "status_code": response.status,
                "headers": dict(response.headers),
                "body": raw.decode("utf-8", errors="replace"),
            }
    except HTTPError as exc:
        raw = exc.read()
        return {
            "status_code": exc.code,
            "headers": dict(exc.headers),
            "body": raw.decode("utf-8", errors="replace"),
        }
    except URLError as exc:
        raise RuntimeError(f"API request failed: {exc.reason}") from exc
