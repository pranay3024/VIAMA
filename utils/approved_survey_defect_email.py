"""Send the defect-report XLSX after raw survey mail and approvals are complete."""

import logging
import time
from datetime import datetime, timezone

from core.config import current_week_number, week_of
from extensions import db
from models.db_models import Survey
from utils.defect_report_api import (
    DefectReportUnavailable,
    download_defect_report,
    find_defect_report_file,
    list_defect_report_files,
)
from utils.email_templates import build_email_body, build_subject
from utils.microsoft_graph_mail import send_microsoft_graph_email


TO_EMAIL = "mail.hunterrr@gmail.com"
FROM_EMAIL = "projects9@adorpower.com"
log = logging.getLogger(__name__)


def defect_candidates_pending():
    """True when at least one survey is waiting for its defect-report mail.

    A database-only check, so the caller can decide whether the RoadVision
    host needs to be contacted at all.
    """
    return (
        db.session.query(Survey.id)
        .filter(
            Survey.raw_video_email_sent_at.is_not(None),
            Survey.defect_report_email_sent_at.is_(None),
            Survey.survey_dates_approved.is_(True),
            Survey.survey_form_approved.is_(True),
            Survey.extracted_survey_start_date.is_not(None),
            Survey.extracted_survey_end_date.is_not(None),
        )
        .first()
        is not None
    )


def pending_defect_email_count():
    """How many surveys are still waiting for their defect-report mail."""
    return (
        db.session.query(Survey.id)
        .filter(
            Survey.raw_video_email_sent_at.is_not(None),
            Survey.defect_report_email_sent_at.is_(None),
            Survey.survey_dates_approved.is_(True),
            Survey.survey_form_approved.is_(True),
            Survey.extracted_survey_start_date.is_not(None),
            Survey.extracted_survey_end_date.is_not(None),
        )
        .count()
    )


def send_approved_survey_defect_email(survey_id, inventory=None):
    """Send the matching defect XLSX once the raw-video email was sent."""
    survey = (
        Survey.query.filter_by(id=survey_id)
        .with_for_update()
        .first()
    )
    if survey is None:
        db.session.rollback()
        return "not_found"

    if survey.defect_report_email_sent_at:
        db.session.rollback()
        return "already_sent"

    if not survey.raw_video_email_sent_at:
        db.session.rollback()
        return "waiting_for_raw_email"

    if not (
        survey.survey_dates_approved
        and survey.survey_form_approved
        and survey.extracted_survey_start_date
        and survey.extracted_survey_end_date
    ):
        db.session.rollback()
        return "waiting_for_approval"

    try:
        if inventory is None:
            inventory = list_defect_report_files()
    except DefectReportUnavailable:
        db.session.rollback()
        return "waiting_for_defect_report"

    report = find_defect_report_file(
        survey.section_no,
        survey.upc_code,
        survey.cycle_no,
        survey.survey_type,
        inventory,
    )
    if report is None:
        db.session.rollback()
        return "waiting_for_defect_report"

    try:
        report_bytes, report_filename = download_defect_report(
            report["label"],
            survey.cycle_no,
            survey.survey_type,
        )
        start_date = survey.extracted_survey_start_date.isoformat()
        end_date = survey.extracted_survey_end_date.isoformat()
        selected_week = week_of(survey.start_time) or current_week_number()
        subject = build_subject(survey, "defect", end_date)
        html_body = build_email_body(
            survey,
            "defect",
            start_date,
            end_date,
            selected_week,
        )
        message_id = send_microsoft_graph_email(
            to_email=TO_EMAIL,
            from_email=FROM_EMAIL,
            subject=subject,
            html_body=html_body,
            attachment_bytes=report_bytes,
            attachment_filename=report_filename,
            message_id=f"<viama-defect-report-{survey.id}@viama.local>",
        )
        survey.defect_report_email_sent_at = datetime.now(
            timezone.utc
        ).replace(tzinfo=None)
        survey.defect_report_email_message_id = message_id
        db.session.commit()
        return "sent"
    except DefectReportUnavailable:
        db.session.rollback()
        return "waiting_for_defect_report"
    except Exception:
        db.session.rollback()
        raise


def process_pending_approved_defect_emails(limit=20, max_seconds=100, inventory=None):
    """Retry a bounded set of defect reports whose raw survey email was sent."""
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
            Survey.raw_video_email_sent_at.is_not(None),
            Survey.defect_report_email_sent_at.is_(None),
            Survey.survey_dates_approved.is_(True),
            Survey.survey_form_approved.is_(True),
            Survey.extracted_survey_start_date.is_not(None),
            Survey.extracted_survey_end_date.is_not(None),
        )
        .order_by(Survey.id.asc())
        .limit(1000)
        .all()
    )
    results = {"checked": 0, "sent": 0, "waiting_for_defect_report": 0, "failed": 0}
    if not candidates:
        return results

    if inventory is None:
        try:
            inventory = list_defect_report_files()
        except DefectReportUnavailable:
            results["waiting_for_defect_report"] = len(candidates)
            return results

    ready_ids = []
    for survey_id, section_no, upc_code, cycle_no, survey_type in candidates:
        if find_defect_report_file(
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
            results["waiting_for_defect_report"] += 1

    for survey_id in ready_ids:
        if time.monotonic() - started_at >= max_seconds:
            results["deferred"] = len(ready_ids) - results["checked"]
            break
        results["checked"] += 1
        try:
            result = send_approved_survey_defect_email(
                survey_id,
                inventory=inventory,
            )
        except Exception:
            log.exception("approved defect email failed for survey %s", survey_id)
            results["failed"] += 1
            continue
        if result in results:
            results[result] += 1

    return results