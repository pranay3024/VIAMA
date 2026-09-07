"""
Print a stored OAuth token as JSON.

    python export_token.py                    # Drive
    python export_token.py token_gmail.pickle # Gmail

Defaults to token_drive.pickle. It used to hardcode ``token.pickle``, which no
longer exists now that Drive and Gmail have separate credentials.

The output contains a live refresh token - do not paste it anywhere shared.
"""

import json
import pickle
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "token_drive.pickle"

with open(path, "rb") as f:
    creds = pickle.load(f)

print(json.dumps({
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": creds.scopes
}, indent=2))
