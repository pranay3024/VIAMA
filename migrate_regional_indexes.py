"""Add indexes used by the regional manager dashboard."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set")

STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS ix_surveys_state_dashboard_start ON surveys (state, show_on_dashboard, start_time)",
    "CREATE INDEX IF NOT EXISTS ix_surveys_state_status ON surveys (state, status)",
    "CREATE INDEX IF NOT EXISTS ix_assignments_state_section_captain_day ON survey_assignments (state, section_no, captain_email, survey_day, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_regional_manager_state_email ON regional_manager_states (manager_email)",
]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in STATEMENTS:
        connection.execute(text(statement))
        print("OK:", statement)

print("Regional dashboard indexes created.")
