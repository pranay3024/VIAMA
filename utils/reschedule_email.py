"""Email the reschedule notice to the NHAI teams when a captain reschedules a survey.

The body is a short notice followed by the project details, which include the
reason the captain saved in the portal.

Recipients: To is the fixed dashcam pair plus the survey's PIU; CC is the RO of
the stretch. For local testing set ``RESCHEDULE_TEST_TO`` and every notice goes
only to that address, with no CC.
"""

import html
import logging
import os
from datetime import datetime, timezone

from datetime import timedelta

from core.config import ist_date, ist_now
from extensions import db
from google_drive import PIU_EMAIL_MAPPING, get_cc_email
from models.db_models import RescheduleReason, Survey
from utils.microsoft_graph_mail import (
    DEFAULT_DRAFT_BCC,
    DEFAULT_DRAFT_TO,
    DEFAULT_MAILBOX,
    find_sent_message_id,
    reply_all_microsoft_graph,
    send_microsoft_graph_email,
    state_bcc,
)

log = logging.getLogger(__name__)

#: Stored when the first notice went out but its id could not be read back.
UNKNOWN_THREAD = "unknown"

def build_subject(survey):
    return f"Rescheduling of Survey – UPC: {survey.upc_code}"


def _saved_reason(survey):
    """The reason the captain saved in the database (captain_status_reason)."""
    return (survey.captain_status_reason or "").strip()


def build_body(survey):
    reason = _saved_reason(survey)
    length = f"{survey.section_length:.2f} km" if survey.section_length else "-"
    rescheduled_on = (
        survey.reschedule_to_date.strftime("%d/%m/%Y") if survey.reschedule_to_date else "-"
    )
    day = survey.survey_day or "-"
    return f"""
<p>Dear Sir,</p>
<p>Please note that the below mentioned survey is rescheduled. Details are provided below.</p>
<p><b>Project Details:</b><br>
Project Name: {html.escape(survey.stretch_code or '-')}<br>
UPC: {html.escape(survey.upc_code or '-')}<br>
RO: {html.escape(survey.ro or '-')}<br>
PIU: {html.escape(survey.piu or '-')}<br>
Length: {length}<br>
Original Schedule Day: {html.escape(day)}<br>
Survey Schedule: This Week (As per Planned Schedule)<br>
<b>Reason for Rescheduling: {html.escape(reason or '-')}</b></p>
<p><b>Rescheduled To: {rescheduled_on}</b></p>
<p>This is for your information and necessary action.</p>
<p>Regards,<br>Team Dashcam-Ador</p>
"""


def _in_current_week(value):
    """Whether a survey's start time falls in the current Monday-to-Sunday week (IST)."""
    if not value:
        return False
    today = ist_now().date()
    monday = today - timedelta(days=today.weekday())
    return monday <= value.date() < monday + timedelta(days=7)


def send_reschedule_email(survey):
    """Send the first notice, or reply in its thread. Returns the thread's message id.

    A repeat reschedule never starts a new mail. If the thread's id is unknown
    (the sent mail could not be read back), nothing is sent for it.
    """
    thread_id = survey.reschedule_thread_message_id
    # A thread only lives within its own week. In a new week the survey starts a
    # new mail, and that mail's id replaces the old one.
    if thread_id and not _in_current_week(survey.start_time):
        thread_id = None
    if thread_id == UNKNOWN_THREAD:
        log.warning(
            "survey %s has a reschedule thread whose id is unknown; not sending",
            survey.id,
        )
        return None
    if thread_id:
        reply_all_microsoft_graph(DEFAULT_MAILBOX, thread_id, build_body(survey))
        return thread_id

    test_to = (os.getenv("RESCHEDULE_TEST_TO") or "").strip()
    if test_to:
        to_email, cc_email, bcc_email = test_to, None, None
    else:
        piu_email = PIU_EMAIL_MAPPING.get((survey.piu or "").strip(), "")
        to_email = ", ".join([*DEFAULT_DRAFT_TO, piu_email] if piu_email else DEFAULT_DRAFT_TO)
        cc_email = get_cc_email(survey.ro) or None
        bcc_email = [*DEFAULT_DRAFT_BCC, *state_bcc(survey.state)] or None

    subject = build_subject(survey)
    send_microsoft_graph_email(
        to_email=to_email,
        cc_emails=cc_email,
        bcc_emails=bcc_email,
        from_email=DEFAULT_MAILBOX,
        subject=subject,
        html_body=build_body(survey),
        attachment_bytes=None,
        attachment_filename=None,
    )
    return find_sent_message_id(DEFAULT_MAILBOX, subject) or UNKNOWN_THREAD


def send_reschedule_email_safely(survey):
    """Send the notice once per reschedule, never raising.

    The row is locked and claimed (reschedule_email_sent_at set and committed)
    before the mail goes out, so a second submit that arrives at the same time
    waits for the lock and then finds the claim already there. The claim is
    released only if the mail never left.

    The reschedule itself is already saved when this runs, so a mail problem
    must not turn the captain's action into an error.
    """
    try:
        locked = Survey.query.filter_by(id=survey.id).with_for_update().first()
        if locked is None or locked.reschedule_email_sent_at:
            db.session.rollback()
            return "already_sent"
        # Automatic reschedule rows have no reason yet. The notice waits until
        # the captain has given one.
        if not (locked.captain_status_reason or "").strip():
            db.session.rollback()
            return "no_reason"
        locked.reschedule_email_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("could not claim the reschedule email for survey %s", survey.id)
        return None

    try:
        thread_id = send_reschedule_email(locked)
        if thread_id:
            locked.reschedule_thread_message_id = thread_id
            db.session.commit()
        return thread_id
    except Exception:
        log.exception("reschedule email failed for survey %s", survey.id)
        db.session.rollback()
        locked.reschedule_email_sent_at = None
        db.session.commit()
        return None


def _date_text(value):
    return value.strftime("%d/%m/%Y") if value else "-"


def build_cancellation_body(survey, reasons, cancelled_on):
    """The reschedule layout with the word changed to cancelled, and every reason listed."""
    length = f"{survey.section_length:.2f} km" if survey.section_length else "-"
    day = survey.survey_day or "-"
    reason_lines = "<br>".join(
        f"{number}. {html.escape(reason)}" for number, reason in enumerate(reasons, start=1)
    ) or "-"
    return f"""
<p>Dear Sir,</p>
<p>Please note that the below mentioned survey is cancelled. Details are provided below.</p>
<p><b>Project Details:</b><br>
Project Name: {html.escape(survey.stretch_code or '-')}<br>
UPC: {html.escape(survey.upc_code or '-')}<br>
RO: {html.escape(survey.ro or '-')}<br>
PIU: {html.escape(survey.piu or '-')}<br>
Length: {length}<br>
Original Schedule Day: {html.escape(day)}<br>
Survey Schedule: This Week (As per Planned Schedule)</p>
<p><b>Cancelled on: {_date_text(cancelled_on)}</b></p>
<p><b>Reasons for Cancellation:</b><br>{reason_lines}</p>
<p>This is for your information and necessary action.</p>
<p>Regards,<br>Team Dashcam-Ador</p>
"""


def send_cancellation_thread_email(survey):
    """Reply in the reschedule thread when the survey is cancelled at week end.

    Like the reschedule replies, this never starts a new mail: without a known
    thread it sends nothing. Returns the thread's message id, or None.
    """
    thread_id = survey.reschedule_thread_message_id
    if not thread_id or thread_id == UNKNOWN_THREAD:
        log.info("survey %s has no known reschedule thread; no cancellation mail", survey.id)
        return None

    reasons = [
        row.reason
        for row in RescheduleReason.query.filter_by(survey_id=survey.id)
        .order_by(RescheduleReason.created_at.asc(), RescheduleReason.id.asc())
        .all()
        if (row.reason or "").strip()
    ]
    cancelled_on = ist_date(survey.captain_status_updated_at)
    reply_all_microsoft_graph(
        DEFAULT_MAILBOX,
        thread_id,
        build_cancellation_body(survey, reasons, cancelled_on),
    )
    return thread_id


def send_cancellation_thread_email_safely(survey):
    """Same as send_cancellation_thread_email, but a failure is logged, never raised."""
    try:
        return send_cancellation_thread_email(survey)
    except Exception:
        log.exception("cancellation thread email failed for survey %s", getattr(survey, "id", None))
        return None

