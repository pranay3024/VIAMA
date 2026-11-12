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

from extensions import db
from google_drive import PIU_EMAIL_MAPPING, get_cc_email
from models.db_models import Survey
from utils.microsoft_graph_mail import (
    DEFAULT_DRAFT_TO,
    DEFAULT_MAILBOX,
    send_microsoft_graph_email,
    state_bcc,
)

log = logging.getLogger(__name__)

def build_subject(survey):
    return f"Rescheduling of Survey – UPC: {survey.upc_code}"


def _saved_reason(survey):
    """The reason the captain saved in the database (captain_status_reason)."""
    return (survey.captain_status_reason or "").strip()


def build_body(survey):
    reason = _saved_reason(survey)
    length = f"{survey.section_length:.2f} km" if survey.section_length else "-"
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
Reason for Cancellation: {html.escape(reason or '-')}</p>
<p>This is for your information and necessary action.</p>
<p>Regards,<br>Team Dashcam-Ador</p>
"""


def send_reschedule_email(survey):
    """Send the notice. Returns the Graph message id, or None when skipped."""
    test_to = (os.getenv("RESCHEDULE_TEST_TO") or "").strip()
    if test_to:
        to_email, cc_email, bcc_email = test_to, None, None
    else:
        piu_email = PIU_EMAIL_MAPPING.get((survey.piu or "").strip(), "")
        to_email = ", ".join([*DEFAULT_DRAFT_TO, piu_email] if piu_email else DEFAULT_DRAFT_TO)
        cc_email = get_cc_email(survey.ro) or None
        bcc_email = state_bcc(survey.state) or None

    return send_microsoft_graph_email(
        to_email=to_email,
        cc_emails=cc_email,
        bcc_emails=bcc_email,
        from_email=DEFAULT_MAILBOX,
        subject=build_subject(survey),
        html_body=build_body(survey),
        attachment_bytes=None,
        attachment_filename=None,
    )


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
        locked.reschedule_email_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("could not claim the reschedule email for survey %s", survey.id)
        return None

    try:
        return send_reschedule_email(locked)
    except Exception:
        log.exception("reschedule email failed for survey %s", survey.id)
        db.session.rollback()
        locked.reschedule_email_sent_at = None
        db.session.commit()
        return None
