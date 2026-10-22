"""Send the approved survey-form email once both approvals are recorded."""

import logging
import time
from datetime import datetime, timezone

from core.config import current_week_number, week_of
from core.config import defect_report_email_enabled
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


#: How many raw emails one dashboard load may send.  The RoadVision API has
#: no callback, so the portal only learns that a file has landed by asking it.
#: The daily cron asks once a day; the dashboard asks on every load, so one
#: email per load is enough to make "the file just arrived" feel immediate.
#: The backlog is the cron's job, not the dashboard's.
INSTANT_TRIGGER_LIMIT = 1

#: Wall-clock budget for that one send.  A dashboard must never hang because
#: a remote API is slow, so this is deliberately short: a send that does not
#: fit is simply left for the cron.
INSTANT_TRIGGER_MAX_SECONDS = 25


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
        if defect_report_email_enabled():
            try:
                from utils.approved_survey_defect_email import (
                    send_approved_survey_defect_email,
                )

                send_approved_survey_defect_email(survey.id)
            except Exception:
                log.exception(
                    "defect report email attempt failed for survey %s",
                    survey.id,
                )
        else:
            log.info(
                "defect report email skipped for survey %s "
                "(DEFECT_REPORT_EMAIL is off)",
                survey.id,
            )
        return "sent"
    except RawXlsxUnavailable:
        db.session.rollback()
        return "waiting_for_raw_xlsx"
    except Exception:
        db.session.rollback()
        raise


def pending_raw_email_candidates():
    """Approved surveys that are still waiting for their raw-video email."""
    return (
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


def pending_raw_email_count():
    """How many surveys are waiting for their raw-video email."""
    return len(pending_raw_email_candidates())


def process_pending_approved_survey_emails(limit=20, max_seconds=240):
    """Try ready surveys without letting older missing sheets starve newer ones."""
    started_at = time.monotonic()
    candidates = pending_raw_email_candidates()

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


def send_pending_raw_emails_now(
    limit=INSTANT_TRIGGER_LIMIT,
    max_seconds=INSTANT_TRIGGER_MAX_SECONDS,
):
    """Send a raw email whose file has just landed in the RoadVision API.

    The RoadVision API has no callback and no "notify me when this file is
    uploaded" endpoint, so the portal only learns that a raw-video file has
    arrived by asking.  The daily cron asks once a day, which is far too slow
    for the people waiting on the mail, so the dashboards ask as well: when
    somebody loads a dashboard the inventory is polled once and anything that
    is ready is sent straight away.

    Two deliberate limits keep a dashboard load cheap and safe:

    * ``limit`` - one email per load.  One is enough for "the file I was
      waiting for just arrived"; the backlog belongs to the cron.
    * ``max_seconds`` - a short wall-clock budget.  A slow remote API must
      not turn into a slow dashboard.

    Never raises.  A failure here has to stay invisible to the page: the
    dashboards call this inside a ``try``/``except`` of their own, and the
    email will still go out on the next load or on the cron.
    """
    from core.engine import _job_lock

    try:
        # One picker at a time.  Two dashboards loading at the same moment
        # would otherwise both poll the inventory and race for the same mail.
        if not _job_lock("viama_raw_email_pickup"):
            return {
                "checked": 0,
                "sent": 0,
                "waiting_for_raw_xlsx": 0,
                "failed": 0,
                "skipped": "another pickup is already running",
            }
        return process_pending_approved_survey_emails(
            limit=limit,
            max_seconds=max_seconds,
        )
    except Exception:
        log.exception("raw video email pickup failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return {
            "checked": 0,
            "sent": 0,
            "waiting_for_raw_xlsx": 0,
            "failed": 0,
            "skipped": "pickup failed",
        }
