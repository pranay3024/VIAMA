from flask import Blueprint
from flask import render_template
from flask import session
from flask import redirect
from flask import request
from models.db_models import db
from models.db_models import SurveyAssignment
from sqlalchemy import case

from utils.visibility import exclude_deleted
from utils.request_params import safe_date, safe_int, safe_week
import os
from werkzeug.utils import secure_filename
from flask import current_app
from google_drive import upload_file_to_drive
from config_drive import (
    DEFECT_REPORT_FOLDER_ID,
    RAW_VIDEO_FOLDER_ID
)

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

roadvision_bp = Blueprint(
    "roadvision_bp",
    __name__
)


@roadvision_bp.route("/roadvision")
def roadvision_dashboard():

    if session.get("role") != "roadvision":
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


    state = request.args.get("state")
    captain_name = request.args.get("captain_name")
    cycle = request.args.get("cycle")
    week = request.args.get("week")
    survey_date = request.args.get("survey_date")

    filtered_query = exclude_deleted(
    Survey.query.filter_by(
    status="completed"
),
    Survey
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

    # -----------------------------------
    # SURVEYS
    # -----------------------------------

    all_surveys = filtered_query.order_by(

    case(

        (
            Survey.roadvision_completed == False,
            1
        ),

        (
            Survey.roadvision_completed == True,
            2
        ),

        else_=3

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
        "roadvision/dashboard.html",
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
        alerts=[],
        resurvey_requests=[]
    )


@roadvision_bp.route("/roadvision/survey/<int:survey_id>")
def roadvision_survey_details(survey_id):

    if session.get("role") != "roadvision":
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
        "roadvision/survey_details.html",
        survey=survey,
        display_start_time=survey.display_start_time,
        display_end_time=survey.display_end_time
    )


@roadvision_bp.route(
    "/roadvision/upload-defect-report/<int:survey_id>",
    methods=["POST"]
)
def upload_defect_report(survey_id):

    if session.get("role") != "roadvision":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    excel = request.files.get("defect_report")

    if not excel:
     return redirect(f"/roadvision/survey/{survey_id}")
    
    file_bytes = excel.read()

    filename = (
       f"{survey.upc_code}"
       f"_Cycle-{survey.cycle_no}"
       f"_Section-{survey.section_no}"
       f"_Defect_Report.xlsx"
)

    result = upload_file_to_drive(
       file_bytes=file_bytes,
       filename=filename,
       folder_id=DEFECT_REPORT_FOLDER_ID,
       mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
    
    survey.defect_report_file = result["view_url"]

    db.session.commit()

    return redirect(f"/roadvision/survey/{survey_id}")


@roadvision_bp.route(
    "/roadvision/upload-raw-video/<int:survey_id>",
    methods=["POST"]
)
def upload_raw_video(survey_id):

    if session.get("role") != "roadvision":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    excel = request.files.get("raw_video_excel")

    if not excel:
        return redirect(f"/roadvision/survey/{survey_id}")

    file_bytes = excel.read()

    filename = (
        f"{survey.upc_code}"
        f"_Cycle-{survey.cycle_no}"
        f"_Section-{survey.section_no}"
        f"_Raw_Video.xlsx"
    )

    result = upload_file_to_drive(
        file_bytes=file_bytes,
        filename=filename,
        folder_id=RAW_VIDEO_FOLDER_ID,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    survey.raw_video_excel_file = result["view_url"]

    db.session.commit()

    return redirect(f"/roadvision/survey/{survey_id}")


@roadvision_bp.route(
    "/roadvision/review/<int:survey_id>",
    methods=["GET", "POST"]
)
def roadvision_review(survey_id):

    if session.get("role") != "roadvision":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    # Already reviewed
    if survey.roadvision_completed:
        return redirect("/roadvision")

    if request.method == "POST":

        remark = request.form.get(
            "remark",
            ""
        ).strip()

        if not remark:
            return render_template(
                "roadvision/review.html",
                survey=survey,
                error="Remark is required."
            )

        survey.roadvision_completed = True
        survey.roadvision_remark = remark
        survey.roadvision_completed_at = datetime.utcnow()

        db.session.commit()

        return redirect("/roadvision")

    return render_template(
        "roadvision/review.html",
        survey=survey
    )


@roadvision_bp.route("/roadvision/remark/<int:survey_id>")
def roadvision_view_remark(survey_id):

    if session.get("role") != "roadvision":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    return render_template(
        "roadvision/view_remark.html",
        survey=survey
    )