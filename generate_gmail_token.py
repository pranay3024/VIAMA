from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose"
]

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    SCOPES
)

creds = flow.run_local_server(port=0)

with open("token_gmail.pickle", "wb") as token:
    pickle.dump(creds, token)

print("✅ Gmail OAuth completed successfully.")