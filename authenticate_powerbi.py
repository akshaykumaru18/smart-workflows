"""Request a Power BI access token for a service principal.

Set these before running:

    export POWERBI_TENANT_ID="your-tenant-id"
    export POWERBI_CLIENT_ID="your-app-client-id"
    export POWERBI_CLIENT_SECRET="your-client-secret"
    python authenticate_powerbi.py
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone

from tools.powerbi_tools import get_access_token, list_workspaces


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> None:
    required = ("POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    token = get_access_token(
        os.environ["POWERBI_TENANT_ID"],
        os.environ["POWERBI_CLIENT_ID"],
        os.environ["POWERBI_CLIENT_SECRET"],
    )
    expires = datetime.fromtimestamp(_claims(token)["exp"], tz=timezone.utc)
    workspaces = list_workspaces(token)

    print(token)
    print(f"expires_at: {expires.isoformat()}", file=sys.stderr)
    print(f"workspaces: {len(workspaces)}", file=sys.stderr)
    for workspace in workspaces:
        print(f"- {workspace.get('name')} ({workspace.get('id')})", file=sys.stderr)


if __name__ == "__main__":
    main()
