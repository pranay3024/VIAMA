"""
Create the DRIVE OAuth token.

Writes ``token_drive.pickle`` - the filename google_drive.py actually reads.
It used to write ``token.pickle``, which nothing has read since Drive and Gmail
were split into separate credentials, so refreshing an expiring Drive token
appeared to succeed while the app carried on using the old one.

Gmail has its own generator: ``python generate_gmail_token.py``.
"""

from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

# Both scopes are requested so this token also works if you ever want to collapse
# Drive and Gmail back into one credential. Only the Drive scope is required.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.compose"
]

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    SCOPES
)

creds = flow.run_local_server(port=0)

with open("token_drive.pickle", "wb") as token:
    pickle.dump(creds, token)

print("✅ OAuth completed successfully.")
print("token_drive.pickle has been created.")
