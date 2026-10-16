
import msal
import requests

CLIENT_ID = ""
TENANT_ID = ""

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = [
    "https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All"
]

DATASET_ID = "3984850c-fcd8-4246-9135-bdf4f9b6aa7e"

app = msal.PublicClientApplication(
    client_id=CLIENT_ID,
    authority=AUTHORITY,
)

flow = app.initiate_device_flow(scopes=SCOPES)

if "user_code" not in flow:
    raise RuntimeError(f"Could not start sign-in: {flow}")

print(flow["message"])

result = app.acquire_token_by_device_flow(flow)

if "access_token" not in result:
    raise RuntimeError(
        result.get("error_description", "Authentication failed")
    )

access_token = result["access_token"]

url = (
    "https://api.powerbi.com/v1.0/myorg/"
    f"datasets/{DATASET_ID}/refreshes"
)

response = requests.post(
    url,
    headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    },
    json={"notifyOption": "MailOnFailure"},
    timeout=30,
)

print("HTTP status:", response.status_code)
print("Response:", response.text)
response.raise_for_status()
