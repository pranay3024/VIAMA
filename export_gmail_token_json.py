"""Print token_gmail.pickle as compact JSON for GMAIL_TOKEN_JSON."""

import json
import os
import pickle

from dotenv import load_dotenv

load_dotenv()

path = os.getenv("GMAIL_TOKEN_PATH", "token_gmail.pickle")
with open(path, "rb") as token_file:
    credentials = pickle.load(token_file)

print(json.dumps(json.loads(credentials.to_json()), separators=(",", ":")))