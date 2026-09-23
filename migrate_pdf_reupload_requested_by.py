"""Add the who-requested PDF re-upload flag to surveys.

Safe to re-run. Run with DATABASE_URL configured so the admin / form-approver
dashboard can show who requested a re-upload.

    python migrate_pdf_reupload_requested_by.py
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "pdf_reupload_requested_by VARCHAR(30)",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete.")