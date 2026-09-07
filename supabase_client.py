"""
Supabase client for the survey-pdfs bucket.

The URL and anon key used to be literals in this file. They are now read from the
environment so the key can be rotated without a code change - but the old values
remain as a fallback, because they are already public in this repository and
failing to boot would be a worse outcome than continuing to use them.

Rotating the anon key is still required: routes/captain.py uses it to upload
*and delete* PDFs, so if the bucket is anon-writable the leaked key can delete
every survey PDF. Audit the bucket's RLS policies at the same time.
"""

import logging
import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

log = logging.getLogger(__name__)

#: Already published in this repository's history - fallback only, not a secret.
_LEGACY_URL = "https://nahcqrxmbzxuixqizdga.supabase.co"
_LEGACY_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5haGNxcnhtYnp4dWl4cWl6ZGdhIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODA5MDgzNDgsImV4cCI6MjA5NjQ4NDM0OH0"
    ".osBVkdRzgbc7daqpmCncrkIahVQnVJGfdisYtXiufO4"
)

url = (os.getenv("SUPABASE_URL") or "").strip() or _LEGACY_URL

key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()

if not key:
    log.warning(
        "SUPABASE_ANON_KEY is not set - falling back to the key that was "
        "hardcoded in supabase_client.py. That key is public; rotate it and set "
        "SUPABASE_ANON_KEY."
    )
    key = _LEGACY_ANON_KEY

supabase = create_client(
    url,
    key
)
