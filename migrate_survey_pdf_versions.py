"""Create the survey_pdf_versions table and backfill it from existing surveys.

Every survey-form PDF the captain uploads now appends a row here, so the
admin / regional / form-approver survey details page can list the original plus
each re-upload with its own timestamp instead of only the latest file.

Backfills one row per survey that has ``end_survey_pdf`` but no history yet,
using ``survey_pdf_uploaded_at`` as the upload time. Re-uploads that already
happened before this table existed cannot be recovered - those Drive files were
deleted - so those surveys start with a single row.

Safe to re-run. Run with DATABASE_URL configured.

    python migrate_survey_pdf_versions.py
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS survey_pdf_versions (
        id SERIAL PRIMARY KEY,
        survey_id INTEGER NOT NULL,
        pdf_url TEXT NOT NULL,
        uploaded_at TIMESTAMP WITHOUT TIME ZONE,
        version_no INTEGER NOT NULL DEFAULT 1,
        uploaded_by_role VARCHAR(30),
        uploaded_by_email VARCHAR(150),
        is_current BOOLEAN NOT NULL DEFAULT FALSE,
        reupload_reason TEXT,
        created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_survey_pdf_versions_survey_id
    ON survey_pdf_versions (survey_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_survey_pdf_versions_survey_version
    ON survey_pdf_versions (survey_id, version_no)
    """,
    """
    INSERT INTO survey_pdf_versions (
        survey_id,
        pdf_url,
        uploaded_at,
        version_no,
        uploaded_by_role,
        uploaded_by_email,
        is_current
    )
    SELECT
        s.id,
        s.end_survey_pdf,
        s.survey_pdf_uploaded_at,
        1,
        'captain',
        s.captain_email,
        TRUE
    FROM surveys s
    WHERE s.end_survey_pdf IS NOT NULL
      AND TRIM(s.end_survey_pdf) <> ''
      AND NOT EXISTS (
          SELECT 1
          FROM survey_pdf_versions v
          WHERE v.survey_id = s.id
      )
    """,
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", " ".join(statement.split())[:80])

    backfilled = connection.execute(
        text("SELECT COUNT(*) FROM survey_pdf_versions")
    ).scalar()

print(f"Migration complete. {backfilled} PDF version(s) on record.")