"""Add the Gemini-extracted survey-form fields to surveys.

Safe to re-run. Run with DATABASE_URL configured before uploading new survey
forms so the extracted start date, AE/IE/SC, PIU and Contractor values persist.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

STATEMENTS = [
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS extracted_survey_start_date DATE",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS survey_start_date_confidence DOUBLE PRECISION",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS extracted_ae_ie_sc_name VARCHAR(255)",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS extracted_piu_name VARCHAR(255)",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS extracted_contractor_agency VARCHAR(255)",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete.")