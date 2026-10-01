"""Collapse duplicate rows in a survey's PDF history.

One upload must produce one history row. ``complete_survey`` used to accept a
resubmitted form - the status guard allows ``video_pending``, which is the very
status a successful upload sets - so a slow double tap wrote one Drive copy and
one history row per tap. Survey 1642 (section 147, cycle 15) collected nine rows
for a single scan, and the admin details page listed all nine.

This keeps the first row (the original the approver saw) and the last row (the
file ``Survey.end_survey_pdf`` points at), deletes the repeats between them and
renumbers what remains. Safe to re-run.

    python repair_duplicate_pdf_versions.py            # dry run, prints the plan
    python repair_duplicate_pdf_versions.py --apply    # write

Run with DATABASE_URL configured.
"""

import argparse
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set - check your .env")

parser = argparse.ArgumentParser()
parser.add_argument(
    "--apply",
    action="store_true",
    help="Write the changes. Without this the script only reports.",
)
parser.add_argument(
    "--survey-id",
    type=int,
    help="Limit the repair to one survey.",
)

args = parser.parse_args()

# Anything with more rows than the survey has re-upload requests plus its
# original PDF has repeats in the history.
FIND_DUPLICATES = """
SELECT v.survey_id,
       count(*)                                        AS row_count,
       COALESCE(s.pdf_reupload_count, 0) + 1           AS expected_rows,
       array_agg(v.id ORDER BY v.version_no, v.id)     AS ids,
       array_agg(v.version_no ORDER BY v.version_no, v.id) AS version_nos,
       s.upc_code,
       s.cycle_no,
       s.section_no
FROM survey_pdf_versions v
JOIN surveys s ON s.id = v.survey_id
GROUP BY v.survey_id, s.pdf_reupload_count, s.upc_code, s.cycle_no, s.section_no
HAVING count(*) > COALESCE(s.pdf_reupload_count, 0) + 1
ORDER BY v.survey_id
"""

DELETE_MIDDLE = """
DELETE FROM survey_pdf_versions
WHERE id = ANY(:ids)
"""

# end_survey_pdf must keep pointing at a row that still exists.
KEEP_CURRENT = """
UPDATE survey_pdf_versions
SET is_current = FALSE
WHERE survey_id = :survey_id
  AND NOT is_current
"""

SET_CURRENT = """
UPDATE survey_pdf_versions
SET is_current = TRUE
WHERE id = :version_id
"""

RENUMBER = """
UPDATE survey_pdf_versions v
SET version_no = :new_no
WHERE v.id = :version_id
"""

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as connection:

    if args.survey_id:
        candidates = [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT v.survey_id,
                           count(*)                                             AS row_count,
                           COALESCE(s.pdf_reupload_count, 0) + 1                AS expected_rows,
                           array_agg(v.id ORDER BY v.version_no, v.id)          AS ids,
                           array_agg(v.version_no ORDER BY v.version_no, v.id)  AS version_nos,
                           s.upc_code,
                           s.cycle_no,
                           s.section_no
                    FROM survey_pdf_versions v
                    JOIN surveys s ON s.id = v.survey_id
                    WHERE v.survey_id = :survey_id
                    GROUP BY v.survey_id, s.pdf_reupload_count,
                             s.upc_code, s.cycle_no, s.section_no
                    """
                ),
                {"survey_id": args.survey_id},
            ).mappings()
        ]
    else:
        candidates = [
            dict(row)
            for row in connection.execute(
                text(FIND_DUPLICATES)
            ).mappings()
        ]

    if not candidates:
        print("No survey has duplicate PDF history rows. Nothing to do.")
        raise SystemExit(0)

    for row in candidates:
        ids = list(row["ids"])
        keep = [ids[0], ids[-1]]
        drop = [row_id for row_id in ids if row_id not in keep]

        print()
        print(
            f"survey {row['survey_id']}  "
            f"upc={row['upc_code']}  "
            f"cycle={row['cycle_no']}  section={row['section_no']}"
        )
        print(
            f"  rows={row['row_count']}  "
            f"expected={row['expected_rows']}  "
            f"reupload_count={row['expected_rows'] - 1}"
        )
        print(f"  version_nos={row['version_nos']}")
        print(f"  ids={ids}")
        print(f"  keeping ids={keep} (first + current)")
        print(f"  deleting ids={drop}")

        if not args.apply:
            continue

        if drop:
            connection.execute(text(DELETE_MIDDLE), {"ids": drop})

        # Whatever survives must have exactly one is_current row, and it must
        # be the one end_survey_pdf resolves to.
        connection.execute(
            text(KEEP_CURRENT), {"survey_id": row["survey_id"]}
        )
        connection.execute(
            text(SET_CURRENT), {"version_id": keep[-1]}
        )

        # Renumber from 1 so the API (which reads version_no) and the details
        # page (which uses list position) agree after the delete.
        for position, version_id in enumerate(keep, start=1):
            connection.execute(
                text(RENUMBER),
                {"version_id": version_id, "new_no": position},
            )

        print("  APPLIED")

if args.apply:
    print()
    print("Repair complete.")
else:
    print()
    print("Dry run only. Re-run with --apply to write.")