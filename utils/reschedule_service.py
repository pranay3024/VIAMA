"""Reschedule a survey: the one place where a reschedule is saved and mailed.

Used by the captain's Reschedule button and by the regional admin's, so both
do exactly the same thing: the survey row for the current week is set to
rescheduled with its reason and date, the assignment leaves the missed list,
and the reschedule notice (or its thread reply) goes out.
"""

from datetime import datetime, timedelta

import pytz

from core.config import stretch_key, stretch_key_expr
from extensions import db
from models.db_models import RescheduleReason, Survey


class RescheduleError(ValueError):
    """A reschedule that cannot be saved. The message is safe to show the user."""


def parse_reschedule_form(form):
    """The reason from a reschedule form, or RescheduleError."""
    reason = (form.get("reason", "") or "").strip()
    if not reason:
        raise RescheduleError("Reschedule reason is mandatory.")
    return reason


def current_week_bounds():
    """Monday 00:00 IST and next Monday 00:00 IST, as naive datetimes (the DB's style)."""
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    week_start = (now_ist - timedelta(days=now_ist.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)
    return (
        week_start.replace(tzinfo=None),
        week_end.replace(tzinfo=None),
        now_ist.replace(tzinfo=None),
    )


def apply_reschedule(assignment, reason):
    """Save a reschedule for this assignment in the current week, then send its notice.

    Raises RescheduleError for a date outside the current week. Returns the
    survey row that was rescheduled.
    """
    week_start, week_end, now_db = current_week_bounds()

    # The current week's row is the same row whatever its status, so a second
    # reschedule updates it rather than adding another row.
    existing_survey = Survey.query.filter(
        Survey.captain_email == assignment.captain_email,
        stretch_key_expr(Survey.section_no) == stretch_key(assignment.section_no),
        Survey.start_time >= week_start,
        Survey.start_time < week_end,
    ).order_by(Survey.id.desc()).first()

    if not existing_survey:
        # Cycle rule: cancelled and rescheduled keep the cycle, anything else moves on.
        previous_survey = Survey.query.filter(
            stretch_key_expr(Survey.section_no) == stretch_key(assignment.section_no),
            Survey.start_time < week_start,
        ).order_by(Survey.start_time.desc(), Survey.id.desc()).first()

        if previous_survey and previous_survey.cycle_no is not None:
            if previous_survey.status in ["cancelled", "rescheduled"]:
                cycle_no = previous_survey.cycle_no
            else:
                cycle_no = previous_survey.cycle_no + 1
        else:
            cycle_no = 1

        existing_survey = Survey(
            captain_email=assignment.captain_email,
            captain_name=assignment.main_person,
            state=assignment.state,
            stretch_code=assignment.stretch_code,
            section_no=assignment.section_no,
            upc_code=assignment.upc_code,
            nh_number=assignment.nh_number,
            ro=assignment.ro,
            piu=assignment.piu,
            survey_day=assignment.survey_day,
            survey_type=assignment.survey_type or "Day",
            section_length=assignment.section_length,
            start_time=now_db,
            end_time=None,
            cycle_no=cycle_no,
            is_resurvey=False,
        )
        db.session.add(existing_survey)

    now_utc = datetime.utcnow()

    # The survey row: rescheduled, with this reason, and visible to everyone.
    existing_survey.status = "rescheduled"
    existing_survey.captain_status = "rescheduled"
    existing_survey.captain_status_reason = reason
    existing_survey.captain_status_updated_at = now_utc
    existing_survey.show_on_dashboard = True
    existing_survey.show_in_teamleader_dashboard = True
    existing_survey.reschedule_email_sent_at = None
    existing_survey.reschedule_to_date = None

    # The assignment leaves the missed list (missed becomes pending again).
    if assignment.status == "missed":
        assignment.status = "pending"
        assignment.alert_acknowledged = False
        assignment.missed_alert = False
        assignment.missed_reason = None
    assignment.captain_status = "rescheduled"
    assignment.captain_status_reason = reason
    assignment.captain_status_updated_at = now_utc

    db.session.add(RescheduleReason(
        survey=existing_survey,
        reason=reason,
        rescheduled_to=None,
    ))
    db.session.commit()

    from utils.reschedule_email import send_reschedule_email_safely

    send_reschedule_email_safely(existing_survey)
    return existing_survey
