"""Synchronize cached survey dates and defect-report sent dates.

Examples:
    python sync_defect_delays.py --limit 20
    python sync_defect_delays.py --limit 50 --retry-errors
    python sync_defect_delays.py --all

The script is resumable: successful records are skipped unless --force is used.
It commits after every survey so an interrupted run can continue safely.
"""

import argparse
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from googleapiclient.errors import HttpError

load_dotenv()

from app import create_app
from extensions import db
from google_drive import get_gmail
from gemini_utils import extract_survey_dates_from_drive
from models.db_models import Survey
from utils.defect_report_delay import (
    build_defect_email_index,
    find_defect_report_email,
    working_days_between,
)


log = logging.getLogger("sync_defect_delays")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync Gemini survey dates and Gmail defect-report dates."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of eligible surveys to process (default: 20).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Process every eligible survey that still needs work.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Include surveys previously marked as error.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run surveys even when their Gemini date and Gmail match are cached.",
    )
    parser.add_argument(
        "--from-date",
        default="2026-08-03",
        help="Only process surveys starting on/after this date (default: 2026-08-03).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear Week 7+ cached results before processing. Use only for a fresh test run.",
    )
    return parser.parse_args()


def is_quota_error(error):
    text = str(error).lower()
    return "quota" in text or "ratelimit" in text or "rate limit" in text


def sync_batch(args):
    app = create_app()
    with app.app_context():
        if args.reset:
            reset_count = Survey.query.filter(
                Survey.start_time >= datetime.strptime(
                    args.from_date, "%Y-%m-%d"
                )
            ).update({
                Survey.extracted_survey_end_date: None,
                Survey.survey_end_date_confidence: None,
                Survey.defect_report_sent_at: None,
                Survey.defect_report_sent_confidence: None,
                Survey.defect_report_email_id: None,
                Survey.defect_report_match_status: None,
                Survey.defect_report_delay_days: None,
            }, synchronize_session=False)
            db.session.commit()
            print(f"Cleared {reset_count} Week 7+ cached delay record(s).")

        query = Survey.query.filter(
            Survey.survey_form_completed.is_(True),
            Survey.task1_completed.is_(True),
            Survey.task2_completed.is_(True),
            Survey.end_survey_pdf.isnot(None),
            Survey.start_time >= datetime.strptime(
                args.from_date, "%Y-%m-%d"
            ),
        ).order_by(Survey.id.asc())

        if not args.force:
    # Only process surveys that have NEVER been checked
           query = query.filter(
             Survey.defect_report_match_status.is_(None)
    )

        if not args.all:
            query = query.limit(max(args.limit, 1))

        surveys = query.all()
        print(
            f"Eligible Week 7+ batch: {len(surveys)} survey(s) "
            f"from {args.from_date}"
        )
        if not surveys:
            print("Nothing to process. Use --retry-errors or --force if needed.")
            return 0

        try:
            gmail = get_gmail()
            print("Indexing Gmail Sent messages once...")
            email_index = build_defect_email_index(gmail)
            print(f"Indexed {len(email_index)} sent defect-report message(s).")
        except Exception as error:
            print(f"Gmail indexing failed: {error}", file=sys.stderr)
            return 1

        counts = {"matched": 0, "not_found": 0, "error": 0}
        for position, survey in enumerate(surveys, start=1):
            print(
                f"[{position}/{len(surveys)}] survey_id={survey.id} "
                f"section={survey.section_no} cycle={survey.cycle_no}",
                flush=True,
            )
            try:
                if args.force or not survey.extracted_survey_end_date:
                    dates = extract_survey_dates_from_drive(
                        survey.end_survey_pdf
                    )
                    survey.extracted_survey_end_date = datetime.strptime(
                        dates["end_date"], "%Y-%m-%d"
                    ).date()
                    survey.survey_end_date_confidence = dates["end_confidence"]

                match = find_defect_report_email(survey, email_index, gmail)
                if not match:
                    survey.defect_report_sent_at = None
                    survey.defect_report_email_id = None
                    survey.defect_report_delay_days = None
                    survey.defect_report_match_status = "not_found"
                    counts["not_found"] += 1
                else:
                    survey.defect_report_sent_at = match["sent_at"]
                    survey.defect_report_sent_confidence = 1.0
                    survey.defect_report_email_id = match["message_id"]
                    raw_delay_days = working_days_between(
                        survey.extracted_survey_end_date,
                        match["sent_at"].date(),
                    )
                    survey.defect_report_delay_days = (
                        max(raw_delay_days - 3, 0)
                    )
                    survey.defect_report_match_status = "matched"
                    counts["matched"] += 1

                db.session.commit()
                print(
                    f"    status={survey.defect_report_match_status} "
                    f"end_date={survey.extracted_survey_end_date} "
                    f"delay_days={survey.defect_report_delay_days}",
                    flush=True,
                )
            except Exception as error:
                db.session.rollback()
                survey = db.session.get(Survey, survey.id)
                survey.defect_report_match_status = "error"
                survey.defect_report_delay_days = None
                db.session.commit()
                counts["error"] += 1
                print(f"    error: {error}", file=sys.stderr, flush=True)
                log.exception("Sync failed for survey %s", survey.id)
                if isinstance(error, HttpError) and is_quota_error(error):
                    print(
                        "Gmail quota reached. Stop now and retry later; "
                        "completed surveys are already saved.",
                        file=sys.stderr,
                    )
                    return 1

        print(
            "Complete: "
            f"matched={counts['matched']} "
            f"not_found={counts['not_found']} "
            f"errors={counts['error']}"
        )
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(sync_batch(parse_args()))
