from flask import Blueprint
from flask import render_template
from flask import session
from flask import redirect
from flask import request
from models.db_models import db
from models.db_models import SurveyAssignment
from sqlalchemy import case, and_, or_
from utils.request_params import safe_date, safe_int, safe_week
import pytz

from datetime import datetime, timedelta

from models.db_models import (
    db,
    User,
    Survey,
    SurveyAssignment
)

from models.db_models import (
    User,
    Survey
)

teamleader_bp = Blueprint(
    "teamleader_bp",
    __name__
)


@teamleader_bp.route("/teamleader")
def teamleader_dashboard():

    if session.get("role") != "team_leader":
        return redirect("/")

    total_captains = User.query.filter_by(
        role="captain"
    ).count()

    ongoing = Survey.query.filter_by(
        status="ongoing"
    ).count()

    completed = Survey.query.filter_by(
        status="completed"
    ).count()

    video_pending = Survey.query.filter_by(
        status="video_pending"
    ).count()

    # -----------------------------------
    # FILTERS
    # -----------------------------------

    status = request.args.get("status")
    state = request.args.get("state")
    captain_name = request.args.get("captain_name")
    cycle = request.args.get("cycle")
    week = request.args.get("week")
    survey_date = request.args.get("survey_date")
    survey_day = request.args.get("survey_day")

    filtered_query = Survey.query.filter_by(
        show_in_teamleader_dashboard=True
    )

    if status:
        filtered_query = filtered_query.filter_by(
            status=status
        )

    if state:
        filtered_query = filtered_query.filter_by(
            state=state
        )

    if captain_name:
        filtered_query = filtered_query.filter_by(
            captain_name=captain_name
        )

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        filtered_query = filtered_query.filter_by(
            cycle_no=cycle_no
        )

    if survey_day:
        filtered_query = filtered_query.filter(
        Survey.survey_day == survey_day
    )

    # -----------------------------------
    # PROJECT WEEK FILTER
    # -----------------------------------

    project_start = datetime(2026, 6, 22)

    week_no = safe_week(week)

    if week_no is not None:

        start = project_start + timedelta(
            days=(week_no - 1) * 7
        )

        end = start + timedelta(days=7)

        filtered_query = filtered_query.filter(
            Survey.start_time >= start,
            Survey.start_time < end
        )

    # -----------------------------------
    # DATE FILTER
    # -----------------------------------

    from_dt = safe_date(survey_date)

    if from_dt:

        to_dt = from_dt + timedelta(days=1)

        filtered_query = filtered_query.filter(
            Survey.start_time >= from_dt,
            Survey.start_time < to_dt
        )

    from sqlalchemy import case, and_, or_

    all_surveys = filtered_query.order_by(

  case(

    (
        and_(
            Survey.survey_form_completed == True,
            Survey.task1_completed == True,
            Survey.task2_completed == False
        ),
        1      # Survey Form YES + Raw Video YES + Final Report NO -> TOP
    ),

    (
        and_(
            Survey.survey_form_completed == True,
            Survey.task1_completed == True,
            Survey.task2_completed == True
        ),
        3      # All three YES -> BOTTOM
    ),

    else_=2      # Everything else (including NO NO NO) -> MIDDLE

),
    Survey.start_time.desc()

).all()

    for survey in all_surveys:

        assignment = SurveyAssignment.query.filter_by(
            section_no=survey.section_no
        ).first()

        if assignment:
            survey.scheduled_day = assignment.survey_day
        else:
            survey.scheduled_day = survey.survey_day

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

        if (
            survey.status == "video_pending"
            and survey.video_pending_start_time
        ):

            survey.upload_duration_minutes = int(
                (
                    datetime.utcnow()
                    - survey.video_pending_start_time
                ).total_seconds() / 60
            )

        elif (
            survey.video_pending_start_time
            and survey.video_upload_time
        ):

            survey.upload_duration_minutes = int(
                (
                    survey.video_upload_time
                    - survey.video_pending_start_time
                ).total_seconds() / 60
            )

        else:

            survey.upload_duration_minutes = 0

    # -----------------------------------
    # DROPDOWNS
    # -----------------------------------

    states = SurveyAssignment.query.with_entities(
        SurveyAssignment.state
    ).distinct().order_by(
        SurveyAssignment.state
    ).all()

    captains = User.query.filter_by(
        role="captain"
    ).order_by(
        User.name
    ).all()

    cycles = db.session.query(
        Survey.cycle_no
    ).distinct().order_by(
        Survey.cycle_no
    ).all()

    today_date = datetime.utcnow()

    total_weeks = (
        (today_date.date() - project_start.date()).days // 7
    ) + 1

    weeks = list(range(1, total_weeks + 1))

    return render_template(
    "teamleader/dashboard.html",
    total_captains=total_captains,
    ongoing=ongoing,
    completed=completed,
    video_pending=video_pending,
    missed=0,
    all_surveys=all_surveys,
    states=states,
    captains=captains,
    cycles=cycles,
    weeks=weeks,

    state=state,
    captain_name=captain_name,
    cycle=cycle,
    week=week,
    survey_day=survey_day,
    survey_date=survey_date,
    status=status,
    days=[
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday"
    ],
    alerts=[],
    resurvey_requests=[]
)

@teamleader_bp.route("/teamleader/task1/<int:survey_id>")
def toggle_task1(survey_id):

    if session.get("role") != "team_leader":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    if survey.task1_completed:
      return redirect("/teamleader")

    survey.task1_completed = True
    survey.task1_completed_at = datetime.now(
    pytz.timezone("Asia/Kolkata")
)

    db.session.commit()

    return ("", 204)



@teamleader_bp.route("/teamleader/task2/<int:survey_id>")
def toggle_task2(survey_id):

    if session.get("role") != "team_leader":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    if survey.task2_completed:
       return redirect("/teamleader")

    survey.task2_completed = True
    survey.task2_completed_at = datetime.now(
    pytz.timezone("Asia/Kolkata")
)

    db.session.commit()

    return ("", 204)



@teamleader_bp.route("/teamleader/survey/<int:survey_id>")
def teamleader_survey_details(survey_id):

    if session.get("role") != "team_leader":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

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

    if (
        survey.status == "video_pending"
        and survey.video_pending_start_time
    ):

        survey.upload_duration_minutes = int(
            (
                datetime.utcnow()
                - survey.video_pending_start_time
            ).total_seconds() / 60
        )

        survey.upload_status_text = "Upload Pending"

    elif (
        survey.video_pending_start_time
        and survey.video_upload_time
    ):

        survey.upload_duration_minutes = int(
            (
                survey.video_upload_time
                - survey.video_pending_start_time
            ).total_seconds() / 60
        )

        survey.upload_status_text = "Upload Duration"

    else:

        survey.upload_duration_minutes = 0
        survey.upload_status_text = ""

    return render_template(
        "teamleader/survey_details.html",
        survey=survey,
        display_start_time=survey.display_start_time,
        display_end_time=survey.display_end_time
    )


@teamleader_bp.route("/teamleader/schedules")
def teamleader_schedules():

    if session.get("role") != "team_leader":
        return redirect("/")

    schedules = SurveyAssignment.query.order_by(
        SurveyAssignment.survey_day,
        SurveyAssignment.section_no
    ).all()

    states = db.session.query(
        SurveyAssignment.state
    ).distinct().all()

    return render_template(
        "teamleader/schedules.html",
        schedules=schedules,
        states=states,
        monday_count=len([s for s in schedules if s.survey_day=="Monday"]),
        tuesday_count=len([s for s in schedules if s.survey_day=="Tuesday"]),
        wednesday_count=len([s for s in schedules if s.survey_day=="Wednesday"]),
        thursday_count=len([s for s in schedules if s.survey_day=="Thursday"]),
        friday_count=len([s for s in schedules if s.survey_day=="Friday"])
    )


@teamleader_bp.route("/teamleader/surveyform/<int:survey_id>")
def toggle_survey_form(survey_id):

    if session.get("role") != "team_leader":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    if survey.survey_form_completed:
        return redirect("/teamleader")

    survey.survey_form_completed = True
    survey.survey_form_completed_at = datetime.now(
    pytz.timezone("Asia/Kolkata")
)

    db.session.commit()

    return ("", 204)