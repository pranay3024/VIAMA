"""Record when the RoadVision raw-video and defect-report files arrived.

The portal learns that a file has landed by reading the RoadVision inventory,
so this is the moment the portal first saw each file.  The upstream does not
expose an upload time, so for files that arrived before this column existed
the value is the first time the portal noticed them - not the true arrival
time.  Anything newer is accurate to the minute.

Both columns are nullable and nothing else changes: a survey without a value
simply has not been seen yet.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


load_dotenv()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise SystemExit("DATABASE_URL is not set")

statements = (
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "raw_video_received_at TIMESTAMP",
    "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS "
    "defect_report_received_at TIMESTAMP",
)

engine = create_engine(database_url, pool_pre_ping=True)
with engine.begin() as connection:
    for statement in statements:
        connection.execute(text(statement))
        print("OK:", statement)

print("Migration complete: raw-video and defect-report received timestamps")
