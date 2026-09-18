"""Survey Form Approver role.

A reviewer who checks uploaded survey forms against the right/wrong criteria
and marks them approved / not approved. The decision is stored on the Survey's
``survey_form_approved`` boolean, which the admin dashboard already displays, so
an admin sees the same value without any extra work.

The dashboard visually follows the admin dashboard (same admin.css classes,
same filter language, same table columns minus the admin-only ones).
"""

import logging

from datetime import datetime, timedelta

from flask import Blueprint
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for

from sqlalchemy import and_, case, or_

from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db
from models.db_models import Survey, SurveyAssignment, User


form_approver_bp = Blueprint(
    "form_approver",
    __name__,
)

log = logging.getLogger(__name__)

_IST_OFFSET = timedelta(hours=5, minutes=30)
_PROJECT_START = datetime(2026, 6, 22)

# Same team -> states mapping the admin dashboard uses.
_TEAM_STATES = {
    "krish": ["MEGHALAYA", "WEST BENGAL", "ASSAM", "BIHAR"],
    "godbole": ["ODISHA"],
    "aspizo": ["UTTAR PRADESH", "JHARKHAND"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_role():
    """Redirect to login unless the current session is a form approver."""
    if session.get("role") != "form_approver":
        return True
    return False


def _current_week_no(utc_now):
    return (
        (utc_now.date() - _PROJECT_START.date()).days // 7
    ) + 1


def _password_alert(message, redirect_to="/form-approver/change-password"):
    """Return an inline alert-and-redirect page (regional.py pattern)."""
    return """
    <script>
    alert("%s");
    window.location.href="%s";
    </script>
    """ % (message, redirect_to)


def _build_query():
    """Base survey query: same visibility contract as the admin dashboard."""
    return Survey.query.filter(
        Survey.show_on_dashboard.is_(True)
    ).filter(
        or_(
            Survey.status != "pending",
            Survey.captain_status.in_(["rescheduled", "cancelled"]),
        )
    )


def _survey_sort_key():
    """Order surveys the same way the admin dashboard does (pending work first)."""
    return case(
        (Survey.pdf_reupload_required.is_(True), 1),
        (Survey.status == "groundwork_completed", 2),
        (Survey.status == "video_uploaded_pending_form", 3),
        (Survey.status == "video_pending", 4),
        (Survey.status == "ongoing", 5),
        (Survey.status == "rescheduled", 6),
        (Survey.status == "cancelled", 7),
        (
            and_(
                Survey.status == "completed",
                Survey.survey_form_completed.is_(False),
            ),
            8,
        ),
        (
            and_(
                Survey.status == "completed",
                Survey.survey_form_completed.is_(True),
            ),
            9,
        ),
        else_=10,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@form_approver_bp.route("/form-approver")
def dashboard():

    if _require_role():
        return redirect("/")

    state = request.args.get("state", "").strip()
    team = request.args.get("team", "").strip()
    cycle = request.args.get("cycle", "").strip()
    week = request.args.get("week", "").strip()
    day = request.args.get("day", "").strip()
    status = request.args.get("status", "").strip()

    # Preserved when the approve / reject buttons submit (hidden inputs).
    filter_args = {
        "state": state,
        "team": team,
        "cycle": cycle,
        "week": week,
        "day": day,
        "status": status,
    }

    utc_now = datetime.utcnow()
    now_ist_naive = (utc_now + _IST_OFFSET).replace(tzinfo=None)

    project_start = datetime(2026, 6, 22)
    current_week_no = _current_week_no(utc_now)
    total_weeks = _current_week_no(utc_now)
    weeks = list(range(1, total_weeks + 1))

    # Week defaults to the current project week, exactly like the admin
    # dashboard: an explicit week wins, any other active filter with no week
    # means "all weeks", and the untouched default view shows the current week.
    other_filters_applied = any([state, team, cycle, day, status])
    if week:
        try:
            week_no = int(week)
        except (TypeError, ValueError):
            week_no = None
    elif not other_filters_applied:
        week_no = current_week_no
    else:
        week_no = None

    query = _build_query()

    if state:
        query = query.filter(Survey.state == state)

    if team in _TEAM_STATES:
        query = query.filter(Survey.state.in_(_TEAM_STATES[team]))

    if cycle:
        try:
            cycle_no = int(cycle)
        except (TypeError, ValueError):
            cycle_no = None
    else:
        cycle_no = None

    if cycle_no is not None:
        query = query.filter(Survey.cycle_no == cycle_no)

    if day:
        query = query.filter(Survey.survey_day == day)

    if status:
        if status == "pdf_reupload_required":
            query = query.filter(Survey.pdf_reupload_required.is_(True))
        elif status == "pdf_delayed":
            query = query.filter(
                Survey.end_time.isnot(None),
                Survey.survey_pdf_uploaded_at.is_(None),
            )
        elif status == "video_delayed":
            query = query.filter(
                Survey.end_time.isnot(None),
                Survey.video_upload_time.is_(None),
            )
        else:
            query = query.filter(Survey.status == status)

    if week_no is not None:
        start = project_start + timedelta(days=(week_no - 1) * 7)
        end = start + timedelta(days=7)
        query = query.filter(
            Survey.start_time >= start,
            Survey.start_time < end,
        )

    all_surveys = query.order_by(
        _survey_sort_key(),
        Survey.start_time.desc(),
        Survey.id.desc(),
    ).all()

    for survey in all_surveys:

        survey.scheduled_day = survey.survey_day

        if (
            survey.captain_status in ["rescheduled", "cancelled"]
            and survey.captain_status_updated_at
        ):
            survey.display_start_time = (
                survey.captain_status_updated_at + _IST_OFFSET
            )
        elif survey.start_time:
            survey.display_start_time = survey.start_time
        else:
            survey.display_start_time = None

        if survey.captain_status in ["rescheduled", "cancelled"]:
            survey.display_end_time = None
        elif survey.end_time:
            survey.display_end_time = survey.end_time + _IST_OFFSET
        else:
            survey.display_end_time = None

        if survey.survey_form_completed_at:
            survey.display_form_completed_time = (
                survey.survey_form_completed_at + _IST_OFFSET
            )
        else:
            survey.display_form_completed_time = None

        if survey.task1_completed_at:
            survey.display_task1_completed_time = (
                survey.task1_completed_at + _IST_OFFSET
            )
        else:
            survey.display_task1_completed_time = None

        if survey.video_upload_time:
            survey.display_video_upload_time = (
                survey.video_upload_time + _IST_OFFSET
            )
        else:
            survey.display_video_upload_time = None

        if survey.survey_pdf_uploaded_at:
            survey.display_pdf_upload_time = (
                survey.survey_pdf_uploaded_at + _IST_OFFSET
            )
        else:
            survey.display_pdf_upload_time = None

        # PDF / video "late" markers use the same next-day 1:00 PM IST
        # deadline the admin dashboard applies.
        survey.pdf_upload_late = False
        if survey.end_time:
            end_time_ist = (
                survey.end_time + _IST_OFFSET
            ).replace(tzinfo=None)
            pdf_deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time(),
            ) + timedelta(hours=13)
            if survey.survey_pdf_uploaded_at:
                pdf_upload_time_ist = (
                    survey.survey_pdf_uploaded_at + _IST_OFFSET
                ).replace(tzinfo=None)
                if pdf_upload_time_ist > pdf_deadline_ist:
                    survey.pdf_upload_late = True
            elif now_ist_naive > pdf_deadline_ist:
                survey.pdf_upload_late = True

        survey.video_upload_late = False
        if survey.end_time:
            end_time_ist = (
                survey.end_time + _IST_OFFSET
            ).replace(tzinfo=None)
            video_deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time(),
            ) + timedelta(hours=13)
            if survey.video_upload_time:
                video_upload_time_ist = (
                    survey.video_upload_time + _IST_OFFSET
                ).replace(tzinfo=None)
                if video_upload_time_ist > video_deadline_ist:
                    survey.video_upload_late = True
            elif now_ist_naive > video_deadline_ist:
                survey.video_upload_late = True

    # Filter dropdown sources.
    distinct_states = (
        SurveyAssignment.query
        .with_entities(SurveyAssignment.state)
        .distinct()
        .all()
    )
    states = sorted({
        row[0] for row in distinct_states if row[0]
    })

    cycles = sorted(
        {survey.cycle_no for survey in all_surveys},
        key=lambda value: (value is None, value or 0),
    )

    success_message = session.pop("form_approver_success", None)

    return render_template(
        "form_approver/dashboard.html",
        all_surveys=all_surveys,
        states=states,
        cycles=cycles,
        weeks=weeks,
        current_week_no=current_week_no,
        filter_args=filter_args,
        success_message=success_message,
    )


# ---------------------------------------------------------------------------
# Survey form approval (the main action for this role)
# ---------------------------------------------------------------------------

@form_approver_bp.route(
    "/form-approver/<int:survey_id>/survey-form-approval",
    methods=["POST"],
)
def survey_form_approval(survey_id):

    if _require_role():
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    action = request.form.get("action", "")
    if action not in ("approve", "reject"):
        return redirect(
            url_for(
                "form_approver.dashboard",
                **{
                    key: value
                    for key, value in request.form.items()
                    if key in ("state", "team", "cycle", "week", "day", "status")
                    and value
                },
            )
        )

    # Once a form is approved it cannot be rolled back - the decision is final.
    if survey.survey_form_approved:
        log.warning(
            "Rejected rollback attempt on survey %s by user %s",
            survey.id,
            session.get("user_id"),
        )
        return redirect(
            url_for(
                "form_approver.dashboard",
                **{
                    key: value
                    for key, value in request.form.items()
                    if key in ("state", "team", "cycle", "week", "day", "status")
                    and value
                },
            )
        )

    survey.survey_form_approved = (action == "approve")
    db.session.commit()

    log.info(
        "Survey form %s for survey %s by user %s",
        action,
        survey.id,
        session.get("user_id"),
    )

    return redirect(
        url_for(
            "form_approver.dashboard",
            **{
                key: value
                for key, value in request.form.items()
                if key in ("state", "team", "cycle", "week", "day", "status")
                and value
            },
        )
    )


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------

@form_approver_bp.route(
    "/form-approver/change-password",
    methods=["GET", "POST"],
)
def change_password():

    if _require_role():
        return redirect("/")

    if request.method == "POST":

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        user = User.query.get(session["user_id"])

        if not check_password_hash(user.password_hash, current_password):
            return _password_alert("Current password is incorrect.")

        if len(str(new_password)) < 6:
            return _password_alert(
                "New password must be at least 6 characters."
            )

        if new_password != confirm_password:
            return _password_alert("Passwords do not match.")

        user.password_hash = generate_password_hash(new_password)
        db.session.commit()

        session["form_approver_success"] = (
            "Password updated successfully."
        )
        return redirect("/form-approver")

    return render_template("form_approver/change_password.html")