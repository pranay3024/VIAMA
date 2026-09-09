import json
import os
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

client_secret_file = os.getenv(
    "GMAIL_CLIENT_SECRET_FILE",
    "gmail_client_secret.json",
)

if not os.path.exists(client_secret_file):
    raise SystemExit(
        f"Missing {client_secret_file}. Download an OAuth Desktop client "
        "JSON from Google Cloud Console and save it with this name."
    )

with open(client_secret_file, encoding="utf-8") as secret_file:
    client_config = json.load(secret_file)

if "installed" not in client_config and "web" not in client_config:
    raise SystemExit(
        f"{client_secret_file} is not an OAuth Desktop/Web client file. "
        "Download an OAuth client ID JSON from Google Cloud Console and "
        "save it as gmail_client_secret.json."
    )

flow = InstalledAppFlow.from_client_secrets_file(
    client_secret_file,
    SCOPES
)

creds = flow.run_local_server(port=0)

with open("token_gmail.pickle", "wb") as token:
    pickle.dump(creds, token)

print("✅ Gmail OAuth completed successfully.")