"""Add a content fingerprint to survey PDF versions.

Safe to re-run. Run with DATABASE_URL configured so repeat submissions of the
same PDF are not appended to the survey's PDF history - see
utils/pdf_versions.fingerprint / is_same_as_current.

    python migrate_pdf_version_content_hash.py

Existing rows keep a NULL content_hash: the fingerprint is taken from the file
the captain selected, and that cannot be recovered for uploads already on
Drive. NULL simply means "no fingerprint on record", so those rows are never
treated as duplicates. They are repaired by
repair_duplicate_pdf_versions.py where the duplication is visible.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    "ALTER TABLE survey_pdf_versions "
    "ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32)",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete.")