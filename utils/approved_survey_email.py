"""Send the approved survey-form email once both approvals are recorded."""

import logging
import time
from datetime import datetime, timezone

from core.config import current_week_number, week_of
from extensions import db
from google_drive import download_file_from_drive
from models.db_models import Survey
from utils.email_templates import build_email_body, build_subject
from utils.microsoft_graph_mail import send_microsoft_graph_email
from utils.raw_xlsx import (
    RawXlsxUnavailable,
    download_raw_xlsx,
    list_raw_xlsx_files,
    raw_xlsx_is_listed,
)


TO_EMAIL = "mail.hunterrr@gmail.com"
FROM_EMAIL = "projects9@adorpower.com"
log = logging.getLogger(__name__)


def send_approved_survey_email(survey_id, inventory=None):
    """Send once both approvals and the matching raw XLSX are available."""
    survey = (
        Survey.query.filter_by(id=survey_id)
        .with_for_update()
        .first()
    )
    if survey is None:
        db.session.rollback()
        return "not_found"

    if survey.raw_video_email_sent_at:
        db.session.rollback()
        return "already_sent"

    if not survey.survey_dates_approved or not survey.survey_form_approved:
        db.session.rollback()
        return "waiting_for_approval"

    if not (
        survey.extracted_survey_start_date
        and survey.extracted_survey_end_date
        and survey.end_survey_pdf
    ):
        db.session.rollback()
        return "missing_survey_data"

    if inventory is None:
        try:
            inventory = list_raw_xlsx_files()
        except RawXlsxUnavailable:
            db.session.rollback()
            return "waiting_for_raw_xlsx"

    if not raw_xlsx_is_listed(
        survey.section_no,
        survey.upc_code,
        survey.cycle_no,
        survey.survey_type,
        inventory,
    ):
        db.session.rollback()
        return "waiting_for_raw_xlsx"

    try:
        start_date = survey.extracted_survey_start_date.isoformat()
        end_date = survey.extracted_survey_end_date.isoformat()
        selected_week = week_of(survey.start_time) or current_week_number()
        subject = build_subject(survey, "raw", end_date)
        html_body = build_email_body(
            survey,
            "raw",
            start_date,
            end_date,
            selected_week,
        )
        attachment_bytes = download_file_from_drive(survey.end_survey_pdf)
        attachment_filename = (
            f"{survey.upc_code or survey.section_no}_Cycle-"
            f"{survey.cycle_no}_Survey_Form.pdf"
        )
        raw_xlsx_bytes, raw_xlsx_filename = download_raw_xlsx(
            survey.section_no,
            survey.cycle_no,
            survey.survey_type,
        )
        message_id = send_microsoft_graph_email(
            to_email=TO_EMAIL,
            from_email=FROM_EMAIL,
            subject=subject,
            html_body=html_body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            additional_attachments=[(raw_xlsx_bytes, raw_xlsx_filename)],
            message_id=f"<viama-survey-{survey.id}@viama.local>",
        )
        survey.raw_video_email_sent_at = datetime.now(
            timezone.utc
        ).replace(tzinfo=None)
        survey.raw_video_email_message_id = message_id
        db.session.commit()
        return "sent"
    except RawXlsxUnavailable:
        db.session.rollback()
        return "waiting_for_raw_xlsx"
    except Exception:
        db.session.rollback()
        raise


def process_pending_approved_survey_emails(limit=20, max_seconds=240):
    """Try ready surveys without letting older missing sheets starve newer ones."""
    started_at = time.monotonic()
    candidates = (
        db.session.query(
            Survey.id,
            Survey.section_no,
            Survey.upc_code,
            Survey.cycle_no,
            Survey.survey_type,
        )
        .filter(
            Survey.survey_dates_approved.is_(True),
            Survey.survey_form_approved.is_(True),
            Survey.raw_video_email_sent_at.is_(None),
            Survey.extracted_survey_start_date.is_not(None),
            Survey.extracted_survey_end_date.is_not(None),
            Survey.end_survey_pdf.is_not(None),
        )
        .order_by(Survey.id.asc())
        .limit(1000)
        .all()
    )

    results = {"checked": 0, "sent": 0, "waiting_for_raw_xlsx": 0, "failed": 0}
    if not candidates:
        return results

    try:
        inventory = list_raw_xlsx_files()
    except RawXlsxUnavailable:
        results["waiting_for_raw_xlsx"] = len(candidates)
        return results

    ready_ids = []
    for survey_id, section_no, upc_code, cycle_no, survey_type in candidates:
        if raw_xlsx_is_listed(
            section_no,
            upc_code,
            cycle_no,
            survey_type,
            inventory,
        ):
            ready_ids.append(survey_id)
            if len(ready_ids) >= max(1, int(limit)):
                break
        else:
            results["waiting_for_raw_xlsx"] += 1

    for survey_id in ready_ids:
        if time.monotonic() - started_at >= max_seconds:
            results["deferred"] = len(ready_ids) - results["checked"]
            break
        results["checked"] += 1
        try:
            result = send_approved_survey_email(survey_id, inventory=inventory)
        except Exception:
            log.exception("approved survey email retry failed for survey %s", survey_id)
            results["failed"] += 1
            continue
        if result in results:
            results[result] += 1

    return results