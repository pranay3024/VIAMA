"""
Create the DRIVE OAuth token.

Writes ``token_drive.pickle`` - the filename google_drive.py actually reads.
It used to write ``token.pickle``, which nothing has read since Drive and Gmail
were split into separate credentials, so refreshing an expiring Drive token
appeared to succeed while the app carried on using the old one.

Gmail has its own generator: ``python generate_gmail_token.py``.
"""

import json
import os
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

client_secret_file = os.getenv(
    "DRIVE_CLIENT_SECRET_FILE",
    "gmail_client_secret.json",
)
if not os.path.exists(client_secret_file):
    raise SystemExit(
        f"Missing {client_secret_file}. Download an OAuth Desktop client "
        "JSON from Google Cloud Console."
    )

with open(client_secret_file, encoding="utf-8") as secret_file:
    client_config = json.load(secret_file)

if "installed" not in client_config and "web" not in client_config:
    raise SystemExit(
        f"{client_secret_file} is not an OAuth Desktop/Web client file. "
        "Use a Google OAuth client JSON, not a service-account key."
    )

flow = InstalledAppFlow.from_client_secrets_file(
    client_secret_file,
    SCOPES
)

creds = flow.run_local_server(port=0)

with open("token_drive.pickle", "wb") as token:
    pickle.dump(creds, token)

print("✅ OAuth completed successfully.")
print("token_drive.pickle has been created.")
