from flask import Blueprint, render_template, session, redirect, request
from datetime import datetime, timedelta
from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)
from models.db_models import db

from utils.request_params import safe_date, safe_int, safe_week

from sqlalchemy import case, and_, or_
from sqlalchemy.orm import defer

from sqlalchemy import case

from models.db_models import (
    User,
    Survey,
    SurveyAssignment,
    RegionalManagerState
)

regional_bp = Blueprint(
    "regional_bp",
    __name__
)


@regional_bp.route("/regional")
def regional_dashboard():

    if session.get("role") != "regional_manager":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # -----------------------------------
    # STATES UNDER THIS MANAGER
    # -----------------------------------

    manager_states = RegionalManagerState.query.filter_by(
        manager_email=user.email
    ).all()

    state_list = [s.state for s in manager_states]

    # -----------------------------------
    # BASE QUERY
    # -----------------------------------

    query = Survey.query.filter(
        Survey.state.in_(state_list),
        Survey.show_on_dashboard == True
    )

    total_captains = User.query.filter(
        User.role == "captain",
        User.region.in_(state_list)
    ).count()

    ongoing = query.filter(
    Survey.status.in_([
        "ongoing",
        "video_uploaded_pending_form",
        "groundwork_completed"
    ])
).count()
    completed = query.filter_by(status="completed").count()
    video_pending = query.filter_by(status="video_pending").count()

    # -----------------------------------
    # FILTERS
    # -----------------------------------

    state = request.args.get("state")
    captain = request.args.get("captain_name")
    status = request.args.get("status")
    week = request.args.get("week")
    cycle = request.args.get("cycle")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    filtered_query = Survey.query.filter(
        Survey.state.in_(state_list),
        Survey.show_on_dashboard == True
    )

    if state:
        filtered_query = filtered_query.filter(
            Survey.state == state
        )

    if captain:
        filtered_query = filtered_query.filter(
            Survey.captain_name == captain
        )

    if status:
        filtered_query = filtered_query.filter(
            Survey.status == status
        )

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        filtered_query = filtered_query.filter(
            Survey.cycle_no == cycle_no
        )

    project_start = datetime(2026, 6, 22)

    today_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_week_no = max(
        1,
        ((today_ist.date() - project_start.date()).days // 7) + 1,
    )

    week_no = safe_week(week)
    if week_no is None and not week:
        week_no = current_week_no

    if week_no is not None:

        week_start = project_start + timedelta(
            days=(week_no - 1) * 7
        )

        week_end = week_start + timedelta(days=7)

        filtered_query = filtered_query.filter(
            Survey.start_time >= week_start,
            Survey.start_time < week_end
        )

    from_dt = safe_date(from_date)
    to_dt = safe_date(to_date)

    if from_dt:
        filtered_query = filtered_query.filter(
            Survey.start_time >= from_dt
        )

    if to_dt:
        filtered_query = filtered_query.filter(
            Survey.start_time <
            to_dt + timedelta(days=1)
        )

    # -----------------------------------
    # SORTING
    # -----------------------------------

    heavy_survey_columns = (
        Survey.end_survey_pdf,
        Survey.end_survey_photo,
        Survey.dashcam_photo,
        Survey.settings_photo,
        Survey.defect_report_file,
        Survey.raw_video_excel_file,
    )

    all_surveys = filtered_query.options(
        *(defer(column) for column in heavy_survey_columns)
    ).order_by(

    case(

        # 1. PDF Re-upload Required
        (
            Survey.pdf_reupload_required.is_(True),
            1
        ),

        # 2. Groundwork Completed
        (
            Survey.status == "groundwork_completed",
            2
        ),

        # 3. Video Uploaded - Form Pending
        (
            Survey.status == "video_uploaded_pending_form",
            3
        ),

        # 4. Video Pending
        (
            Survey.status == "video_pending",
            4
        ),

        # 5. Ongoing
        (
            Survey.status == "ongoing",
            5
        ),

        # 6. Rescheduled
        (
            or_(
                Survey.status == "rescheduled",
                Survey.captain_status == "rescheduled"
            ),
            6
        ),

        # 7. Cancelled
        (
            or_(
                Survey.status == "cancelled",
                Survey.captain_status == "cancelled"
            ),
            7
        ),

        # 8. Completed - Tasks Pending
        (
            and_(
                Survey.status == "completed",
                or_(
                    Survey.task1_completed.is_(False),
                    Survey.task2_completed.is_(False),
                    Survey.survey_form_completed.is_(False)
                )
            ),
            8
        ),

        # 9. Fully Completed
        (
            and_(
                Survey.status == "completed",
                Survey.task1_completed.is_(True),
                Survey.task2_completed.is_(True),
                Survey.survey_form_completed.is_(True)
            ),
            9
        ),

        # 10. Everything else
        else_=10

    ),

    Survey.start_time.desc()

).all()

    assignment_rows = SurveyAssignment.query.filter(
        SurveyAssignment.state.in_(state_list)
    ).order_by(
        SurveyAssignment.id.desc()
    ).all()
    latest_assignments = {}
    for assignment in assignment_rows:
        key = (
            assignment.section_no,
            assignment.captain_email,
            assignment.survey_day,
        )
        latest_assignments.setdefault(key, assignment)

    for survey in all_surveys:

     assignment = latest_assignments.get((
        survey.section_no,
        survey.captain_email,
        survey.survey_day,
    ))

     if assignment:
        # Make assignment information available
        # directly on the Survey object for Jinja
        survey.captain_status = assignment.captain_status
        survey.captain_status_reason = (
            assignment.captain_status_reason
        )
     else:
        survey.captain_status = None
        survey.captain_status_reason = None

    # -----------------------------------
    # PREPARE DISPLAY DATA
    # -----------------------------------

    for survey in all_surveys:

        if survey.start_time:
            survey.display_start_time = survey.start_time
        else:
            survey.display_start_time = None

        if survey.end_time:
            survey.display_end_time = survey.end_time
        else:
            survey.display_end_time = None

        survey.pdf_upload_late = False
        survey.video_upload_late = False
        if survey.end_time:
            end_time_ist = (
                survey.end_time + timedelta(hours=5, minutes=30)
            ).replace(tzinfo=None)
            upload_deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time(),
            ) + timedelta(hours=14)
            if survey.survey_pdf_uploaded_at:
                survey.pdf_upload_late = (
                    survey.survey_pdf_uploaded_at
                    + timedelta(hours=5, minutes=30)
                ).replace(tzinfo=None) > upload_deadline_ist
            if survey.video_upload_time:
                survey.video_upload_late = (
                    survey.video_upload_time
                    + timedelta(hours=5, minutes=30)
                ).replace(tzinfo=None) > upload_deadline_ist

        if (
            survey.status == "video_pending"
            and survey.video_pending_start_time
        ):
            survey.upload_duration_minutes = int(
                (
                    datetime.utcnow() -
                    survey.video_pending_start_time
                ).total_seconds() / 60
            )

            survey.upload_status_text = "Upload Pending"

        elif (
            survey.video_pending_start_time
            and survey.video_upload_time
        ):
            survey.upload_duration_minutes = int(
                (
                    survey.video_upload_time -
                    survey.video_pending_start_time
                ).total_seconds() / 60
            )

            survey.upload_status_text = "Upload Duration"

        else:
            survey.upload_duration_minutes = 0
            survey.upload_status_text = ""

    # -----------------------------------
    # REGIONAL MISSED SURVEY LOGIC
    # -----------------------------------

    alerts = []

    missed = SurveyAssignment.query.filter(
        SurveyAssignment.status == "missed",
        SurveyAssignment.state.in_(state_list)
    ).count()

        # -----------------------------------
    # DROPDOWNS
    # -----------------------------------

    states = [(s,) for s in state_list]

    captains = User.query.filter(
        User.role == "captain",
        User.region.in_(state_list)
    ).order_by(
        User.name
    ).all()

    today = datetime.utcnow()

    total_weeks = (
        (today.date() - project_start.date()).days // 7
    ) + 1

    weeks = list(
        range(
            1,
            total_weeks + 1
        )
    )

    cycles = (
        db.session.query(
            Survey.cycle_no
        )
        .distinct()
        .order_by(
            Survey.cycle_no
        )
        .all()
    )

    return render_template(

        "regional/dashboard.html",

        total_captains=total_captains,
        ongoing=ongoing,
        completed=completed,
        video_pending=video_pending,
        missed=missed,

        all_surveys=all_surveys,
        timedelta=timedelta,

        states=states,
        captains=captains,

        weeks=weeks,
        current_week_no=current_week_no,
        cycles=cycles,

        alerts=alerts,
        resurvey_requests=[]

    )


@regional_bp.route("/regional/missed")
def regional_missed():

    if session.get("role") != "regional_manager":
        return redirect("/")

    user = User.query.get(session["user_id"])

    manager_states = RegionalManagerState.query.filter_by(
        manager_email=user.email
    ).all()

    state_list = [s.state for s in manager_states]

    missed_surveys = SurveyAssignment.query.filter(
        SurveyAssignment.status == "missed",
        SurveyAssignment.state.in_(state_list)
    ).order_by(
        SurveyAssignment.survey_day,
        SurveyAssignment.section_no
    ).all()

    return render_template(
        "regional/missed.html",
        missed_surveys=missed_surveys
    )

@regional_bp.route("/regional/schedules")
def regional_schedules():

    if session.get("role") != "regional_manager":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    manager_states = RegionalManagerState.query.filter_by(
        manager_email=user.email
    ).all()

    state_list = [
        s.state
        for s in manager_states
    ]

    schedules = SurveyAssignment.query.filter(
        SurveyAssignment.state.in_(state_list)
    ).order_by(
        SurveyAssignment.survey_day,
        SurveyAssignment.section_no
    ).all()

    states = [
        (state,)
        for state in state_list
    ]

    return render_template(
        "regional/schedules.html",
        schedules=schedules,
        states=states,
        monday_count=len([s for s in schedules if s.survey_day=="Monday"]),
        tuesday_count=len([s for s in schedules if s.survey_day=="Tuesday"]),
        wednesday_count=len([s for s in schedules if s.survey_day=="Wednesday"]),
        thursday_count=len([s for s in schedules if s.survey_day=="Thursday"]),
        friday_count=len([s for s in schedules if s.survey_day=="Friday"])
    )


@regional_bp.route("/regional/survey/<int:survey_id>")
def regional_survey_details(survey_id):

    if session.get("role") != "regional_manager":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    manager_states = RegionalManagerState.query.filter_by(
        manager_email=user.email
    ).all()

    state_list = [
        s.state
        for s in manager_states
    ]

    survey = Survey.query.get_or_404(
        survey_id
    )

    if survey.state not in state_list:
        return "Unauthorized", 403

    # -----------------------------------
    # IST DISPLAY TIME
    # -----------------------------------

    if survey.start_time:

        survey.display_start_time = (
            survey.start_time +
            timedelta(hours=5, minutes=30)
        )

    else:

        survey.display_start_time = None

    if survey.end_time:

        survey.display_end_time = (
            survey.end_time +
            timedelta(hours=5, minutes=30)
        )

    else:

        survey.display_end_time = None

    # -----------------------------------
    # UPLOAD DURATION
    # -----------------------------------

    if (
        survey.status == "video_pending"
        and survey.video_pending_start_time
    ):

        survey.upload_duration_minutes = int(
            (
                datetime.utcnow() -
                survey.video_pending_start_time
            ).total_seconds() / 60
        )

        survey.upload_status_text = (
            "Upload Pending"
        )

    elif (
    survey.status in [
        "video_uploaded_pending_form",
        "groundwork_completed",
        "completed",
    ]
    and survey.video_pending_start_time
    and survey.video_upload_time
):

        survey.upload_duration_minutes = int(
            (
                survey.video_upload_time -
                survey.video_pending_start_time
            ).total_seconds() / 60
        )

        survey.upload_status_text = (
            "Upload Duration"
        )

    else:

        survey.upload_duration_minutes = 0
        survey.upload_status_text = ""

    return render_template(
        "regional/survey_details.html",
        survey=survey,
        display_start_time=survey.display_start_time,
        display_end_time=survey.display_end_time
    )


@regional_bp.route(
    "/regional/change-password",
    methods=["POST"]
)
def regional_change_password():

    if session.get("role") != "regional_manager":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    # Defaulted to "" rather than None: check_password_hash(hash, None) raises
    # AttributeError on None.encode, and len(None) below raises TypeError, so a
    # form posted with a field missing returned a 500 instead of the alert.
    current_password = request.form.get(
        "current_password",
        ""
    )

    new_password = request.form.get(
        "new_password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    # -----------------------------------
    # VERIFY CURRENT PASSWORD
    # -----------------------------------

    if not check_password_hash(
        user.password_hash,
        current_password
    ):

        return """
        <script>
        alert("Current password is incorrect.");
        window.location.href="/regional";
        </script>
        """

    # -----------------------------------
    # MATCH PASSWORDS
    # -----------------------------------

    if new_password != confirm_password:

        return """
        <script>
        alert("New passwords do not match.");
        window.location.href="/regional";
        </script>
        """

    # -----------------------------------
    # MINIMUM LENGTH
    # -----------------------------------

    if len(new_password) < 8:

        return """
        <script>
        alert("Password must be at least 8 characters.");
        window.location.href="/regional";
        </script>
        """

    # -----------------------------------
    # UPDATE PASSWORD
    # -----------------------------------

    user.password_hash = generate_password_hash(
        new_password
    )

    db.session.commit()

    return """
    <script>
    alert("Password changed successfully.");
    window.location.href="/regional";
    </script>
    """