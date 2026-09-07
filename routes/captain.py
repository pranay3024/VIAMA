from flask import Blueprint
from flask import render_template
from flask import session
from flask import redirect
from models.db_models import SurveyAssignment
from flask import request
from extensions import db
from models.db_models import Survey
from models.db_models import SurveySchedule 
from datetime import datetime
from supabase_client import supabase
import uuid
import pytz
from datetime import datetime, timedelta
from flask import flash
import os
from utils.image_compressor import compress_image
from utils.visibility import exclude_deleted
from utils.request_params import safe_count, safe_int



from models.db_models import User


captain_bp = Blueprint(
    "captain",
    __name__
)

from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)

@captain_bp.route("/captain")
def captain_dashboard():

    if session.get("role") != "captain":
        return redirect("/")

    return redirect("/captain-home")


@captain_bp.route("/checklist", methods=["GET", "POST"])
def checklist():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # =========================================================
    # SHOW CHECKLIST
    # =========================================================

    if request.method == "GET":
        return render_template(
            "captain/checklist.html"
        )

    # =========================================================
    # GET SELECTED ASSIGNMENT
    # =========================================================

    assignment_id = session.get("assignment_id")

    if not assignment_id:
        return redirect("/captain-home")

    assignment = SurveyAssignment.query.filter_by(
        id=assignment_id,
        captain_email=user.email
    ).first()

    if not assignment:
        session.pop("assignment_id", None)
        return "Assignment not found"

    # =========================================================
    # CURRENT WEEK
    # MONDAY 00:00 → NEXT MONDAY 00:00
    # =========================================================

    ist = pytz.timezone("Asia/Kolkata")

    now_ist = datetime.now(ist)

    current_week_start = (
        now_ist - timedelta(days=now_ist.weekday())
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    current_week_end = (
        current_week_start + timedelta(days=7)
    )

    # DB timestamps are naive
    week_start = current_week_start.replace(
        tzinfo=None
    )

    week_end = current_week_end.replace(
        tzinfo=None
    )

    now_db = now_ist.replace(
        tzinfo=None
    )

    # =========================================================
    # FIND SURVEY ONLY FROM CURRENT WEEK
    #
    # IMPORTANT:
    # We only look at the current week.
    # Previous week's survey will NEVER be modified.
    # =========================================================

    existing_survey = Survey.query.filter(

        Survey.captain_email == user.email,

        Survey.section_no == assignment.section_no,

        Survey.upc_code == assignment.upc_code,

        Survey.stretch_code == assignment.stretch_code,

        Survey.start_time >= week_start,

        Survey.start_time < week_end

    ).order_by(
        Survey.id.desc()
    ).first()

    # =========================================================
    # RESCHEDULED / CANCELLED CURRENT-WEEK SURVEY
    #
    # SAME ROW MUST BE REUSED
    #
    # DO NOT CREATE ANOTHER SURVEY.
    # =========================================================

    if existing_survey and existing_survey.captain_status in [
        "rescheduled",
        "cancelled"
    ]:

        # -----------------------------------------------------
        # START THE SAME CURRENT-WEEK SURVEY
        # -----------------------------------------------------

        existing_survey.status = "ongoing"

        existing_survey.captain_status = "pending"

        existing_survey.start_time = now_db

        existing_survey.end_time = None

        # -----------------------------------------------------
        # RESET SURVEY PROCESS
        # -----------------------------------------------------

        existing_survey.video_uploaded = False

        existing_survey.survey_form_completed = False

        existing_survey.task1_completed = False

        existing_survey.task2_completed = False

        existing_survey.pdf_reupload_required = False

        existing_survey.pdf_reupload_reason = None

        existing_survey.video_pending_start_time = None

        existing_survey.video_upload_time = None

        # -----------------------------------------------------
        # RESET CAPTAIN DECISION
        # -----------------------------------------------------

        assignment.captain_status = "pending"

        assignment.captain_status_reason = None

        assignment.captain_status_updated_at = (
            datetime.utcnow()
        )

        assignment.status = "started"

        # -----------------------------------------------------
        # SAVE
        # -----------------------------------------------------

        db.session.commit()

        # -----------------------------------------------------
        # CONTINUE WITH SAME SURVEY
        # -----------------------------------------------------

        session.pop("assignment_id", None)

        session.pop("is_resurvey", None)

        session["survey_id"] = existing_survey.id

        return redirect("/recording")

    # =========================================================
    # ANY OTHER CURRENT-WEEK SURVEY EXISTS
    #
    # Example:
    # completed
    # ongoing
    # video_pending
    # groundwork_completed
    #
    # Do NOT start it again.
    # =========================================================

    if existing_survey:

        flash(
            f"This stretch has already been started this week. "
            f"Section {existing_survey.section_no}, "
            f"Cycle {existing_survey.cycle_no}.",
            "warning"
        )

        return redirect("/captain-home")

    # =========================================================
    # NO CURRENT-WEEK SURVEY
    #
    # A NEW SURVEY ROW IS REQUIRED.
    #
    # NOW DETERMINE THE CORRECT CYCLE.
    # =========================================================

    latest_survey = Survey.query.filter(

        Survey.section_no == assignment.section_no,

        Survey.upc_code == assignment.upc_code,

        Survey.stretch_code == assignment.stretch_code

    ).order_by(

        Survey.start_time.desc(),

        Survey.id.desc()

    ).first()

    # =========================================================
    # CYCLE LOGIC
    # =========================================================
    #
    # IMPORTANT:
    #
    # Previous cycle 6 COMPLETED
    #     → new cycle 7
    #
    # Previous cycle 6 CANCELLED
    #     → new cycle 6
    #
    # Previous cycle 6 RESCHEDULED
    #     → new cycle 6
    #
    # Previous cycle 6 MISSED
    #     → new cycle 6
    #
    # Because cancelled / rescheduled / missed means
    # the actual survey cycle never happened.
    # =========================================================

    if latest_survey and latest_survey.cycle_no is not None:

        previous_cycle = latest_survey.cycle_no

        previous_status = latest_survey.status

        previous_captain_status = (
            latest_survey.captain_status
        )

        # -----------------------------------------------------
        # SURVEY DID NOT ACTUALLY HAPPEN
        # -----------------------------------------------------

        if (
            previous_status in [
                "cancelled",
                "rescheduled",
                "missed"
            ]
            or
            previous_captain_status in [
                "cancelled",
                "rescheduled"
            ]
        ):

            cycle_no = previous_cycle

        # -----------------------------------------------------
        # SURVEY ACTUALLY HAPPENED
        # -----------------------------------------------------

        else:

            cycle_no = previous_cycle + 1

    else:

        cycle_no = 1

    # =========================================================
    # UPLOAD DASHCAM PHOTO
    # =========================================================

    dashcam_photo = (
        request.files.get("dashcam_photo")
        or request.files.get("gallery_photo")
    )

    dashcam_url = None

    if dashcam_photo:

        import os
        import tempfile

        from google_drive import upload_file_to_drive

        ext = os.path.splitext(
            dashcam_photo.filename
        )[1].lower()

        dashcam_name = (
            str(uuid.uuid4()) + ext
        )

        # -----------------------------------------------------
        # CREATE TEMP FILE
        # -----------------------------------------------------

        fd, temp_path = tempfile.mkstemp(
            suffix=ext
        )

        os.close(fd)

        # -----------------------------------------------------
        # SAVE IMAGE
        # -----------------------------------------------------

        dashcam_photo.save(temp_path)

        # -----------------------------------------------------
        # COMPRESS IMAGE
        # -----------------------------------------------------

        compressed_path = compress_image(
            temp_path
        )

        # -----------------------------------------------------
        # READ COMPRESSED IMAGE
        # -----------------------------------------------------

        with open(
            compressed_path,
            "rb"
        ) as f:

            image_bytes = f.read()

        # -----------------------------------------------------
        # UPLOAD TO GOOGLE DRIVE
        # -----------------------------------------------------

        result = upload_file_to_drive(
            file_bytes=image_bytes,
            filename=dashcam_name,
            folder_id="1W1SCf0_E28VdM8zfzzjFOR-aJL2-e0tT",
            mime_type="image/jpeg"
        )

        dashcam_url = result["image_url"]

        # -----------------------------------------------------
        # CLEANUP
        # -----------------------------------------------------

        try:
            os.remove(temp_path)
        except Exception:
            pass

        try:
            os.remove(compressed_path)
        except Exception:
            pass

    # =========================================================
    # CREATE NEW SURVEY
    # =========================================================

    survey = Survey(

        captain_email=user.email,

        captain_name=user.name,

        state=assignment.state,

        stretch_code=assignment.stretch_code,

        section_no=assignment.section_no,

        upc_code=assignment.upc_code,

        nh_number=assignment.nh_number,

        ro=assignment.ro,

        piu=assignment.piu,

        survey_day=session.get(
            "survey_day",
            assignment.survey_day
        ),

        survey_type=session.get(
            "survey_type",
            assignment.survey_type or "Day"
        ),

        section_length=assignment.section_length,

        status="ongoing",

        captain_status="pending",

        start_time=now_db,

        dashcam_photo=dashcam_url,

        cycle_no=cycle_no,

        is_resurvey=False
    )

    db.session.add(survey)

    # =========================================================
    # UPDATE ASSIGNMENT
    # =========================================================

    if assignment.status == "missed":

        assignment.alert_acknowledged = False

        assignment.missed_alert = False

        assignment.missed_reason = None

    assignment.status = "started"

    assignment.captain_status = "pending"

    assignment.captain_status_reason = None

    assignment.captain_status_updated_at = (
        datetime.utcnow()
    )

    # =========================================================
    # SAVE
    # =========================================================

    db.session.commit()

    # =========================================================
    # CURRENT SURVEY
    # =========================================================

    session.pop("assignment_id", None)

    session.pop("is_resurvey", None)

    session["survey_id"] = survey.id

    return redirect("/recording")
        


@captain_bp.route("/recording")
def recording():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    survey_id = session.get("survey_id")

    if not survey_id:
        return redirect("/captain-home")

    # -----------------------------------
    # LOAD THE EXACT SURVEY
    # -----------------------------------

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email
    ).first()

    if not survey:
        session.pop("survey_id", None)
        return redirect("/captain-home")

    # -----------------------------------
    # ONLY ALLOW ACTIVE SURVEYS
    # -----------------------------------

    if survey.status not in [
    "ongoing",
    "groundwork_completed",
    "video_pending",
    "video_uploaded_pending_form"
]:
        session.pop("survey_id", None)
        return redirect("/captain-home")

    # -----------------------------------
    # DISPLAY START TIME
    # -----------------------------------

    display_time = None

    if survey.start_time:

        display_time = (
            survey.start_time +
            timedelta(hours=5, minutes=30)
        )

    return render_template(
        "captain/recording.html",
        survey=survey,
        start_time=display_time.strftime(
            "%d %b %Y, %I:%M %p"
        ) if display_time else "-"
    )




@captain_bp.route("/groundwork-complete", methods=["POST"])
def groundwork_complete():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    survey_id = session.get("survey_id")

    if not survey_id:
        return redirect("/captain-home")

    # -----------------------------------
    # LOAD EXACT CURRENT SURVEY
    # -----------------------------------

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email
    ).first()

    if not survey:
        session.pop("survey_id", None)
        return redirect("/captain-home")

    # -----------------------------------
    # COMPLETE GROUNDWORK FOR THIS SURVEY
    # -----------------------------------

    if survey.status == "ongoing":

        survey.status = "groundwork_completed"

        # Stop survey timing here
        if survey.end_time is None:
            survey.end_time = datetime.now(
                pytz.timezone("Asia/Kolkata")
            )

        db.session.commit()

    return redirect("/recording")


@captain_bp.route(
    "/select-stretch",
    methods=["GET", "POST"]
)
def select_stretch():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    assignments = SurveyAssignment.query.filter_by(
        captain_email=user.email
    ).all()

    if request.method == "POST":

        assignment_id = safe_int(
            request.form.get("assignment_id"),
            minimum=1
        )

        assignment = None

        if assignment_id is not None:

            assignment = SurveyAssignment.query.filter_by(
                id=assignment_id,
                captain_email=user.email
            ).first()

        if not assignment:

            flash(
                "Please choose one of your assigned stretches.",
                "warning"
            )

            return redirect("/select-stretch")

        session.pop("survey_id", None)

        session["assignment_id"] = assignment.id

        session["survey_day"] = request.form.get(
            "survey_day",
            assignment.survey_day
        )

        session["survey_type"] = request.form.get(
            "survey_type",
            assignment.survey_type
        )

        return redirect("/survey-details")

    return render_template(
        "captain/select_stretch.html",
        user=user,
        assignments=assignments
    )

   
    

@captain_bp.route("/survey-details")
def survey_details():

    if session.get("role") != "captain":
        return redirect("/")

    assignment_id = session.get("assignment_id")

    if not assignment_id:
        return redirect("/select-stretch")

    assignment = SurveyAssignment.query.filter_by(
        id=assignment_id,
        captain_email=User.query.get(
            session["user_id"]
        ).email
    ).first()

    if not assignment:
        session.pop("assignment_id", None)
        return redirect("/select-stretch")

    return render_template(
        "captain/survey_details.html",
        assignment=assignment,
        survey_day=session.get(
            "survey_day",
            assignment.survey_day
        ),
        survey_type=session.get(
            "survey_type",
            assignment.survey_type
        )
    )

@captain_bp.route(
    "/complete-survey",
    methods=["GET", "POST"]
)
def complete_survey():

    if session.get("role") != "captain":
        return redirect("/")

    survey_id = session.get("survey_id")

    if not survey_id:
        return redirect("/captain-home")

    survey = Survey.query.filter_by(
        id=survey_id,
        captain_email=User.query.get(session["user_id"]).email
    ).first()

    if not survey:
        session.pop("survey_id", None)
        return redirect("/captain-home")

    if request.method == "POST":

        if request.form.get("verification_done"):

            if survey.status not in [
            "groundwork_completed",
            "video_pending",
            "video_uploaded_pending_form"
]:

                flash(
                    "Please click 'Complete Groundwork' before finishing the survey.",
                    "warning"
                )

                return redirect("/recording")

            pdf = request.files.get("survey_pdf")

            if not pdf:
                flash("Please upload the survey PDF.", "warning")
                return redirect("/recording")

            # -----------------------------------
            # PDF VALIDATION
            # -----------------------------------

            pdf.seek(0, os.SEEK_END)
            size = pdf.tell()
            pdf.seek(0)

            if size > 50 * 1024 * 1024:

                flash(
                    "PDF size cannot exceed 50 MB.",
                    "warning"
                )

                return redirect("/recording")

            if (
                pdf.mimetype != "application/pdf"
                or not pdf.filename.lower().endswith(".pdf")
            ):

                flash(
                    "Only PDF files are allowed.",
                    "warning"
                )

                return redirect("/recording")

            # -----------------------------------
            # UPLOAD PDF
            # -----------------------------------

            from google_drive import upload_file_to_drive
            from config_drive import PDF_FOLDER_ID
            from pdf_utils import optimize_pdf

            pdf_bytes = pdf.read()

            pdf_bytes = optimize_pdf(pdf_bytes)

            pdf_filename = (
                f"{survey.upc_code}"
                f"_Cycle-{survey.cycle_no}"
                f"_Section-{survey.section_no}.pdf"
            )

            result = upload_file_to_drive(
                file_bytes=pdf_bytes,
                filename=pdf_filename,
                folder_id=PDF_FOLDER_ID,
                mime_type="application/pdf"
            )

            pdf_url = result["view_url"]

            survey.end_survey_pdf = pdf_url
            survey.survey_pdf_uploaded_at = datetime.utcnow()

            # -----------------------------------
            # UPDATE SURVEY STATUS
            # -----------------------------------

            if survey.video_uploaded:

                survey.status = "completed"

            else:

                survey.video_uploaded = False
                survey.status = "video_pending"

            survey.video_pending_start_time = datetime.utcnow()

            # -----------------------------------
            # UPDATE ASSIGNMENT
            # -----------------------------------

            assignment = SurveyAssignment.query.filter(
               SurveyAssignment.section_no == survey.section_no,
               SurveyAssignment.stretch_code == survey.stretch_code,
               SurveyAssignment.state == survey.state
               ).first()

            if assignment:
             assignment.status = "completed"

            db.session.commit()

            # -----------------------------------
            # CLEAR ONLY CURRENT SURVEY
            # -----------------------------------

            session.pop("survey_id", None)

            return redirect("/captain-home")

    return redirect("/captain-home")

from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)

@captain_bp.route(
    "/change-password",
    methods=["GET", "POST"]
)
def change_password():

    if session.get("role") != "captain":
        return redirect("/")

    if request.method == "POST":

        # See routes/regional.py:regional_change_password - a missing field
        # arrives as None and blows up inside check_password_hash.
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

        user = User.query.get(
            session["user_id"]
        )

        if not check_password_hash(
            user.password_hash,
            current_password
        ):
            return "Current password is incorrect"

        if new_password != confirm_password:
            return "Passwords do not match"

        user.password_hash = (
            generate_password_hash(
                new_password
            )
        )

        db.session.commit()

        session["success_message"] = (
            "Password Updated Successfully"
        )

        return redirect(
            "/select-stretch"
        )

    return render_template(
        "captain/change_password.html"
    )



@captain_bp.route("/captain-home")
def captain_home():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # -----------------------------------
    # ALL ONGOING / ACTIVE SURVEYS
    # -----------------------------------

    ongoing_surveys = Survey.query.filter(
        Survey.captain_email == user.email,
        Survey.status.in_([
            "ongoing",
            "groundwork_completed",
            "video_pending",
            "video_uploaded_pending_form"
        ])
    ).order_by(
        Survey.id.desc()
    ).all()

    # -----------------------------------
    # ASSIGNMENTS
    # -----------------------------------

    assigned_count = SurveyAssignment.query.filter_by(
        captain_email=user.email
    ).count()

    # -----------------------------------
    # PENDING / ACTIVE COUNT
    # -----------------------------------

    pending_count = Survey.query.filter(
        Survey.captain_email == user.email,
        Survey.status.in_([
            "ongoing",
            "groundwork_completed",
            "video_uploaded_pending_form",
            "video_pending"
        ])
    ).count()

    # -----------------------------------
    # COMPLETED COUNT
    # -----------------------------------

    completed_count = Survey.query.filter_by(
        captain_email=user.email,
        status="completed"
    ).count()

    pdf_reupload_count = Survey.query.filter(
    Survey.captain_email == user.email,
    Survey.pdf_reupload_required.is_(True)
).count()
    
    return render_template(
        "captain/home.html",

        user=user,

        pdf_reupload_count=pdf_reupload_count,

        assigned_count=assigned_count,

        pending_count=pending_count,

        completed_count=completed_count,

        ongoing_surveys=ongoing_surveys
    )



@captain_bp.route("/continue-survey/<int:survey_id>")
def continue_survey(survey_id):

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email,
        Survey.status.in_([
            "ongoing",
            "groundwork_completed",
            "video_pending",
            "video_uploaded_pending_form"
        ])
    ).first()

    if not survey:
        flash(
            "This survey is no longer available.",
            "warning"
        )
        return redirect("/captain-home")

    if survey.status == "video_pending":
     flash(
        "Form is already uploaded. Please upload the video.",
        "warning"
    )
     return redirect("/captain-home")

    # -----------------------------------
    # SELECT EXACT SURVEY
    # -----------------------------------

    session["survey_id"] = survey.id

    return redirect("/recording")

@captain_bp.route(
    "/video-counts/<int:survey_id>",
    methods=["GET", "POST"]
)
def video_counts(survey_id):

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # -----------------------------------
    # LOAD EXACT SURVEY
    # -----------------------------------

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email
    ).first()

    if not survey:
        return redirect("/captain-home")

    # -----------------------------------
    # ONLY ACTIVE / VIDEO-PENDING SURVEY
    # -----------------------------------

    if survey.status not in [
        "ongoing",
        "groundwork_completed",
        "video_uploaded_pending_form",
        "video_pending"
    ]:
        return redirect("/captain-home")

    # -----------------------------------
    # MAKE THIS THE CURRENT SURVEY
    # -----------------------------------

    session["survey_id"] = survey.id

    # -----------------------------------
    # SAVE VIDEO COUNTS
    # -----------------------------------

    if request.method == "POST":

        survey.ir_lhs_count = safe_count(
            request.form.get("ir_lhs_count")
        )

        survey.mcw_lhs_count = safe_count(
            request.form.get("mcw_lhs_count")
        )

        survey.service_lhs_count = safe_count(
            request.form.get("service_lhs_count")
        )

        survey.slip_lhs_count = safe_count(
            request.form.get("slip_lhs_count")
        )

        survey.ir_rhs_count = safe_count(
            request.form.get("ir_rhs_count")
        )

        survey.mcw_rhs_count = safe_count(
            request.form.get("mcw_rhs_count")
        )

        survey.service_rhs_count = safe_count(
            request.form.get("service_rhs_count")
        )

        survey.slip_rhs_count = safe_count(
            request.form.get("slip_rhs_count")
        )

        survey.video_uploaded = True
        survey.video_upload_time = datetime.utcnow()

        # -----------------------------------
        # STATUS
        # -----------------------------------

        if survey.status == "video_pending":

            survey.status = "completed"

        elif survey.status in [
            "ongoing",
            "groundwork_completed"
        ]:

            survey.status = "video_uploaded_pending_form"

        db.session.commit()

        # -----------------------------------
        # DON'T LOSE OTHER ACTIVE SURVEYS
        # -----------------------------------

        session.pop("survey_id", None)

        return redirect("/captain-home")

    return render_template(
        "captain/video_counts.html",
        survey=survey
    )

@captain_bp.route("/pending-uploads")
def pending_uploads():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    surveys = exclude_deleted(
        Survey.query.filter(
            Survey.captain_email == user.email,

            db.or_(
                Survey.status.in_([
                    "ongoing",
                    "groundwork_completed",
                    "video_uploaded_pending_form",
                    "video_pending"
                ]),

                Survey.pdf_reupload_required == True
            )
        ),
        Survey
    ).order_by(
        Survey.id.desc()
    ).all()

    return render_template(
        "captain/pending_uploads.html",
        surveys=surveys
    )

@captain_bp.route(
    "/upload-video/<int:survey_id>",
    methods=["POST"]
)
def upload_video(survey_id):

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # -----------------------------------
    # LOAD EXACT SURVEY
    # -----------------------------------

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email
    ).first()

    if not survey:
        return redirect("/pending-uploads")

    video_status = request.form.get("video_status")

    # -----------------------------------
    # VIDEO UPLOAD CONFIRMED
    # -----------------------------------

    if video_status == "yes":

        # Make this the currently active survey
        session["survey_id"] = survey.id

        return redirect(
            f"/video-counts/{survey.id}"
        )

    # -----------------------------------
    # VIDEO NOT UPLOADED
    # -----------------------------------

    return redirect("/pending-uploads")

@captain_bp.route("/completed-surveys")
def completed_surveys():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    surveys = Survey.query.filter(
        Survey.captain_email == user.email,
        Survey.status == "completed"
    ).order_by(
        Survey.end_time.desc()
    ).all()

    return render_template(
        "captain/completed_surveys.html",
        surveys=surveys
    )

@captain_bp.route(
    "/completed-survey/<int:survey_id>"
)
def completed_survey_details(survey_id):

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    survey = Survey.query.filter(
        Survey.id == survey_id,
        Survey.captain_email == user.email,
        Survey.status == "completed"
    ).first()

    if not survey:
        flash(
            "Completed survey not found.",
            "warning"
        )
        return redirect("/completed-surveys")

    return render_template(
        "captain/completed_survey_details.html",
        survey=survey
    )

@captain_bp.route(
    "/unable-to-survey",
    methods=["GET","POST"]
)
def unable_to_survey():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    assignments = SurveyAssignment.query.filter_by(
        captain_email=user.email
    ).all()

    if request.method == "POST":

        # Scoped to this captain's own assignments, and tolerant of an id that
        # no longer resolves - the bare .get() returned None and the next line
        # 500ed on None.missed_reason.
        assignment_id = safe_int(
            request.form.get("assignment_id"),
            minimum=1
        )

        assignment = None

        if assignment_id is not None:
            assignment = SurveyAssignment.query.filter_by(
                id=assignment_id,
                captain_email=user.email
            ).first()

        if not assignment:
            flash("That assignment could not be found.", "warning")
            return redirect("/unable-to-survey")

        assignment.missed_reason = request.form.get("reason", "")

        db.session.commit()

        return redirect(
            "/captain-home"
        )

    return render_template(
        "captain/unable_to_survey.html",
        assignments=assignments
    )


@captain_bp.route("/test123")
def test123():
    return "TEST WORKING"


@captain_bp.route(
    "/request-resurvey/<int:survey_id>",
    methods=["POST"]
)
def request_resurvey(survey_id):

    if session.get("role") != "captain":
     return redirect("/")

    survey = Survey.query.get_or_404(
        survey_id
    )

    survey.resurvey_requested = True

    db.session.commit()

    return redirect(
        "/completed-surveys"
    )


@captain_bp.route("/captain/resurvey")
def captain_resurvey():

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    assignments = SurveyAssignment.query.filter_by(
        captain_email=user.email,
        survey_enabled=True
    ).order_by(
        SurveyAssignment.section_no
    ).all()

    return render_template(
        "captain/resurvey_select.html",
        user=user,
        assignments=assignments
    )


@captain_bp.route(
    "/captain/resurvey/start/<int:assignment_id>"
)
def start_resurvey(assignment_id):

    if session.get("role") != "captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    assignment = SurveyAssignment.query.filter_by(
        id=assignment_id,
        captain_email=user.email
    ).first_or_404()

    # -----------------------------------
    # STARTING A NEW RE-SURVEY
    # -----------------------------------

    # Do not keep an old active survey selected
    session.pop("survey_id", None)

    session["assignment_id"] = assignment.id

    session["survey_day"] = assignment.survey_day

    session["survey_type"] = (
        (assignment.survey_type or "Day")
        + " Re-Survey"
    )

    session["is_resurvey"] = True

    return redirect("/survey-details")



@captain_bp.route(
    "/reupload-survey-pdf/<int:survey_id>",
    methods=["GET", "POST"]
)
def reupload_survey_pdf(survey_id):

    if not session.get("user_id"):
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    user = User.query.get(session["user_id"])

    if survey.captain_email != user.email:
        return redirect("/pending-uploads")

    if request.method == "POST":

        pdf = request.files.get("survey_pdf")

        if not pdf:
            flash(
                "Please select a PDF.",
                "warning"
            )
            return redirect(
                f"/reupload-survey-pdf/{survey_id}"
            )

        # -----------------------------------
        # PDF VALIDATION
        # -----------------------------------

        if (
            pdf.mimetype != "application/pdf"
            or not pdf.filename.lower().endswith(".pdf")
        ):

            flash(
                "Only PDF files are allowed.",
                "warning"
            )

            return redirect(
                f"/reupload-survey-pdf/{survey_id}"
            )

        # -----------------------------------
        # PDF SIZE
        # -----------------------------------

        pdf.seek(0, os.SEEK_END)

        size = pdf.tell()

        pdf.seek(0)

        if size > 50 * 1024 * 1024:

            flash(
                "PDF size cannot exceed 50 MB.",
                "warning"
            )

            return redirect(
                f"/reupload-survey-pdf/{survey_id}"
            )

        # -----------------------------------
        # SAVE OLD PDF URL
        # -----------------------------------

        old_pdf_url = survey.end_survey_pdf

        # -----------------------------------
        # GOOGLE DRIVE
        # -----------------------------------

        from google_drive import (
            upload_file_to_drive,
            delete_file_from_drive
        )

        from config_drive import PDF_FOLDER_ID

        from pdf_utils import optimize_pdf

        # -----------------------------------
        # READ + OPTIMIZE PDF
        # -----------------------------------

        pdf_bytes = pdf.read()

        pdf_bytes = optimize_pdf(pdf_bytes)

        # -----------------------------------
        # FILE NAME
        # -----------------------------------

        pdf_filename = (
            f"{survey.upc_code}"
            f"_Cycle-{survey.cycle_no}"
            f"_Section-{survey.section_no}.pdf"
        )

        # -----------------------------------
        # UPLOAD NEW PDF TO GOOGLE DRIVE
        # -----------------------------------

        try:

            result = upload_file_to_drive(
                file_bytes=pdf_bytes,
                filename=pdf_filename,
                folder_id=PDF_FOLDER_ID,
                mime_type="application/pdf"
            )

            new_pdf_url = result["view_url"]

            print(
                "NEW PDF UPLOADED:",
                new_pdf_url
            )

        except Exception as e:

            print(
                "GOOGLE DRIVE PDF UPLOAD ERROR:",
                e
            )

            flash(
                "PDF upload failed. Please try again.",
                "danger"
            )

            return redirect(
                f"/reupload-survey-pdf/{survey_id}"
            )

        # -----------------------------------
        # DELETE OLD PDF FROM GOOGLE DRIVE
        # -----------------------------------

        if old_pdf_url:

            try:

                import re

                match = re.search(
                    r"/d/([a-zA-Z0-9_-]+)",
                    old_pdf_url
                )

                if match:

                    old_file_id = match.group(1)

                    delete_file_from_drive(
                        old_file_id
                    )

                    print(
                        "OLD PDF DELETED:",
                        old_file_id
                    )

                else:

                    print(
                        "OLD PDF FILE ID NOT FOUND:",
                        old_pdf_url
                    )

            except Exception as e:

                # Do NOT fail the re-upload
                # if old PDF deletion fails.

                print(
                    "OLD PDF DELETE ERROR:",
                    e
                )

        # -----------------------------------
        # UPDATE DATABASE
        # -----------------------------------

        survey.end_survey_pdf = new_pdf_url

        survey.pdf_reupload_required = False

        survey.pdf_reupload_reason = None

        db.session.commit()

        # -----------------------------------
        # SUCCESS
        # -----------------------------------

        flash(
            "Survey PDF re-uploaded successfully.",
            "success"
        )

        return redirect(
            "/pending-uploads"
        )

    # -----------------------------------
    # GET
    # -----------------------------------

    return render_template(
        "captain/reupload_pdf.html",
        survey=survey
    )


@captain_bp.route(
    "/captain/cancel/<int:assignment_id>",
    methods=["POST"]
)
def mark_cancelled(assignment_id):

    # =========================================================
    # CHECK CAPTAIN LOGIN
    # =========================================================

    if session.get("role") != "captain":
        return redirect("/")

    assignment = SurveyAssignment.query.get_or_404(
        assignment_id
    )

    user = User.query.get(
        session["user_id"]
    )

    if not user or assignment.captain_email != user.email:
        return redirect("/")


    # =========================================================
    # REASON IS MANDATORY
    # =========================================================

    reason = request.form.get(
        "reason",
        ""
    ).strip()

    if not reason:

        flash(
            "Cancellation reason is mandatory.",
            "error"
        )

        return redirect("/captain")


    # =========================================================
    # CURRENT WEEK
    # MONDAY 00:00 → NEXT MONDAY 00:00
    # =========================================================

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    now_ist = datetime.now(ist)

    current_week_start = (
        now_ist
        - timedelta(
            days=now_ist.weekday()
        )
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    current_week_end = (
        current_week_start
        + timedelta(days=7)
    )


    # =========================================================
    # DB USES NAIVE DATETIME
    # =========================================================

    week_start = current_week_start.replace(
        tzinfo=None
    )

    week_end = current_week_end.replace(
        tzinfo=None
    )

    now_db = now_ist.replace(
        tzinfo=None
    )


    # =========================================================
    # FIND SURVEY ONLY IN CURRENT WEEK
    #
    # STATUS DOES NOT MATTER.
    #
    # If a row already exists this week,
    # update that SAME row.
    # =========================================================

    existing_survey = Survey.query.filter(

        Survey.captain_email ==
        assignment.captain_email,

        Survey.section_no ==
        assignment.section_no,

        Survey.upc_code ==
        assignment.upc_code,

        Survey.stretch_code ==
        assignment.stretch_code,

        Survey.start_time >= week_start,

        Survey.start_time < week_end

    ).order_by(

        Survey.id.desc()

    ).first()


    # =========================================================
    # NO CURRENT-WEEK SURVEY
    #
    # CREATE NEW CANCELLED ROW
    # =========================================================

    if not existing_survey:

        # =====================================================
        # FIND PREVIOUS SURVEY
        # =====================================================

        previous_survey = Survey.query.filter(

            Survey.section_no ==
            assignment.section_no,

            Survey.upc_code ==
            assignment.upc_code,

            Survey.stretch_code ==
            assignment.stretch_code,

            Survey.start_time < week_start

        ).order_by(

            Survey.start_time.desc(),
            Survey.id.desc()

        ).first()


        # =====================================================
        # CALCULATE CYCLE
        #
        # cancelled    → SAME CYCLE
        # rescheduled  → SAME CYCLE
        #
        # ANY OTHER STATUS → NEXT CYCLE
        # =====================================================

        if (
            previous_survey
            and previous_survey.cycle_no is not None
        ):

            if previous_survey.status in [
                "cancelled",
                "rescheduled"
            ]:

                cycle_no = (
                    previous_survey.cycle_no
                )

            else:

                cycle_no = (
                    previous_survey.cycle_no + 1
                )

        else:

            cycle_no = 1


        # =====================================================
        # CREATE CURRENT-WEEK CANCELLED SURVEY
        # =====================================================

        existing_survey = Survey(

            captain_email=user.email,

            captain_name=user.name,

            state=assignment.state,

            stretch_code=assignment.stretch_code,

            section_no=assignment.section_no,

            upc_code=assignment.upc_code,

            nh_number=assignment.nh_number,

            ro=assignment.ro,

            piu=assignment.piu,

            survey_day=assignment.survey_day,

            survey_type=(
                assignment.survey_type
                or "Day"
            ),

            section_length=assignment.section_length,

            status="cancelled",

            captain_status="cancelled",

            captain_status_reason=reason,

            captain_status_updated_at=datetime.utcnow(),

            start_time=now_db,

            end_time=None,

            cycle_no=cycle_no,

            is_resurvey=False,

            show_on_dashboard=True,

            show_in_teamleader_dashboard=True
        )

        db.session.add(
            existing_survey
        )


    # =========================================================
    # CURRENT-WEEK SURVEY ALREADY EXISTS
    #
    # DO NOT CREATE ANOTHER ROW.
    #
    # UPDATE THE SAME ROW.
    # =========================================================

    else:

        existing_survey.status = (
            "cancelled"
        )

        existing_survey.captain_status = (
            "cancelled"
        )

        existing_survey.captain_status_reason = (
            reason
        )

        existing_survey.captain_status_updated_at = (
            datetime.utcnow()
        )

        existing_survey.show_on_dashboard = (
            True
        )

        existing_survey.show_in_teamleader_dashboard = (
            True
        )


    # =========================================================
    # UPDATE ASSIGNMENT
    # =========================================================

    if assignment.status == "missed":

        assignment.status = "pending"

        assignment.alert_acknowledged = False

        assignment.missed_alert = False

        assignment.missed_reason = None


    assignment.captain_status = (
        "cancelled"
    )

    assignment.captain_status_reason = (
        reason
    )

    assignment.captain_status_updated_at = (
        datetime.utcnow()
    )


    # =========================================================
    # ENSURE SURVEY VALUES
    # =========================================================

    existing_survey.status = (
        "cancelled"
    )

    existing_survey.captain_status = (
        "cancelled"
    )

    existing_survey.captain_status_reason = (
        reason
    )

    existing_survey.captain_status_updated_at = (
        datetime.utcnow()
    )

    existing_survey.show_on_dashboard = (
        True
    )

    existing_survey.show_in_teamleader_dashboard = (
        True
    )


    # =========================================================
    # SAVE
    # =========================================================

    db.session.commit()


    # =========================================================
    # SUCCESS MESSAGE
    # =========================================================

    session["success_message"] = (
        "Survey cancelled successfully. "
        "The status has been updated."
    )

    return redirect("/captain")

@captain_bp.route(
    "/captain/reschedule/<int:assignment_id>",
    methods=["POST"]
)
def mark_rescheduled(assignment_id):

    # =========================================================
    # CHECK CAPTAIN LOGIN
    # =========================================================

    if session.get("role") != "captain":
        return redirect("/")

    assignment = SurveyAssignment.query.get_or_404(
        assignment_id
    )

    user = User.query.get(
        session["user_id"]
    )

    if not user or assignment.captain_email != user.email:
        return redirect("/")


    # =========================================================
    # REASON IS MANDATORY
    # =========================================================

    reason = request.form.get(
        "reason",
        ""
    ).strip()

    if not reason:

        flash(
            "Reschedule reason is mandatory.",
            "error"
        )

        return redirect("/captain")


    # =========================================================
    # CURRENT WEEK
    # MONDAY 00:00 → NEXT MONDAY 00:00
    # =========================================================

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    now_ist = datetime.now(ist)

    current_week_start = (
        now_ist
        - timedelta(
            days=now_ist.weekday()
        )
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    current_week_end = (
        current_week_start
        + timedelta(days=7)
    )


    # =========================================================
    # DB USES NAIVE DATETIME
    # =========================================================

    week_start = current_week_start.replace(
        tzinfo=None
    )

    week_end = current_week_end.replace(
        tzinfo=None
    )

    now_db = now_ist.replace(
        tzinfo=None
    )


    # =========================================================
    # FIND SURVEY ONLY IN CURRENT WEEK
    #
    # STATUS DOES NOT MATTER.
    #
    # If a survey row already exists this week,
    # update that SAME row.
    # =========================================================

    existing_survey = Survey.query.filter(

        Survey.captain_email ==
        assignment.captain_email,

        Survey.section_no ==
        assignment.section_no,

        Survey.upc_code ==
        assignment.upc_code,

        Survey.stretch_code ==
        assignment.stretch_code,

        Survey.start_time >= week_start,

        Survey.start_time < week_end

    ).order_by(

        Survey.id.desc()

    ).first()


    # =========================================================
    # NO CURRENT-WEEK SURVEY
    #
    # CREATE NEW RESCHEDULED ROW
    # =========================================================

    if not existing_survey:

        # =====================================================
        # FIND PREVIOUS SURVEY
        #
        # Cycle rule:
        #
        # cancelled    → SAME cycle
        # rescheduled  → SAME cycle
        #
        # Anything else → NEXT cycle
        # =====================================================

        previous_survey = Survey.query.filter(

            Survey.section_no ==
            assignment.section_no,

            Survey.upc_code ==
            assignment.upc_code,

            Survey.stretch_code ==
            assignment.stretch_code,

            Survey.start_time < week_start

        ).order_by(

            Survey.start_time.desc(),
            Survey.id.desc()

        ).first()


        # =====================================================
        # CALCULATE CYCLE
        # =====================================================

        if (
            previous_survey
            and previous_survey.cycle_no is not None
        ):

            if previous_survey.status in [
                "cancelled",
                "rescheduled"
            ]:

                # Cancelled / Rescheduled
                # DOES NOT consume the cycle

                cycle_no = (
                    previous_survey.cycle_no
                )

            else:

                # Any other status consumes the cycle

                cycle_no = (
                    previous_survey.cycle_no + 1
                )

        else:

            cycle_no = 1


        # =====================================================
        # CREATE CURRENT-WEEK RESCHEDULED SURVEY
        # =====================================================

        existing_survey = Survey(

            captain_email=user.email,

            captain_name=user.name,

            state=assignment.state,

            stretch_code=assignment.stretch_code,

            section_no=assignment.section_no,

            upc_code=assignment.upc_code,

            nh_number=assignment.nh_number,

            ro=assignment.ro,

            piu=assignment.piu,

            survey_day=assignment.survey_day,

            survey_type=(
                assignment.survey_type
                or "Day"
            ),

            section_length=assignment.section_length,

            status="rescheduled",

            captain_status="rescheduled",

            captain_status_reason=reason,

            captain_status_updated_at=datetime.utcnow(),

            start_time=now_db,

            end_time=None,

            cycle_no=cycle_no,

            is_resurvey=False,

            show_on_dashboard=True,

            show_in_teamleader_dashboard=True
        )

        db.session.add(
            existing_survey
        )


    # =========================================================
    # CURRENT-WEEK SURVEY ALREADY EXISTS
    #
    # DO NOT CREATE ANOTHER ROW.
    #
    # UPDATE THE SAME ROW.
    # =========================================================

    else:

        existing_survey.status = (
            "rescheduled"
        )

        existing_survey.captain_status = (
            "rescheduled"
        )

        existing_survey.captain_status_reason = (
            reason
        )

        existing_survey.captain_status_updated_at = (
            datetime.utcnow()
        )

        existing_survey.show_on_dashboard = (
            True
        )

        existing_survey.show_in_teamleader_dashboard = (
            True
        )


    # =========================================================
    # UPDATE ASSIGNMENT
    # =========================================================

    if assignment.status == "missed":

        assignment.status = "pending"

        assignment.alert_acknowledged = False

        assignment.missed_alert = False

        assignment.missed_reason = None


    assignment.captain_status = (
        "rescheduled"
    )

    assignment.captain_status_reason = (
        reason
    )

    assignment.captain_status_updated_at = (
        datetime.utcnow()
    )


    # =========================================================
    # ENSURE SURVEY VALUES
    # =========================================================

    existing_survey.status = (
        "rescheduled"
    )

    existing_survey.captain_status = (
        "rescheduled"
    )

    existing_survey.captain_status_reason = (
        reason
    )

    existing_survey.captain_status_updated_at = (
        datetime.utcnow()
    )

    existing_survey.show_on_dashboard = (
        True
    )

    existing_survey.show_in_teamleader_dashboard = (
        True
    )


    # =========================================================
    # SAVE
    # =========================================================

    db.session.commit()


    # =========================================================
    # SUCCESS
    # =========================================================

    session["success_message"] = (
        "Survey rescheduled successfully. "
        "The status has been updated."
    )

    return redirect("/captain")