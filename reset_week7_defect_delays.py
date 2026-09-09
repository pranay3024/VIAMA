"""Clear cached defect-delay results from Week 7 onward.

Week 7 starts on 2026-08-03. Survey completion flags and source files are
preserved; only cached Gemini/Gmail delay results are cleared.
"""

from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

import os

load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

engine = create_engine(database_url, pool_pre_ping=True)
statement = text("""
    UPDATE surveys
    SET extracted_survey_end_date = NULL,
        survey_end_date_confidence = NULL,
        defect_report_sent_at = NULL,
        defect_report_sent_confidence = NULL,
        defect_report_email_id = NULL,
        defect_report_match_status = NULL,
        defect_report_delay_days = NULL
    WHERE start_time >= :week7_start
""")

with engine.begin() as connection:
    result = connection.execute(
        statement,
        {"week7_start": datetime(2026, 8, 3)},
    )
    print(f"Reset {result.rowcount} Week 7+ survey record(s).")