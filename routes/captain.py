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

    user = User.query.get(
        session["user_id"]
    )

    ongoing = Survey.query.filter(

    Survey.captain_email == user.email,

    Survey.status.in_([
        "ongoing",
        "groundwork_completed",
        "video_uploaded_pending_form"
    ])

).order_by(
    Survey.id.desc()
    ).first()

    schedule = SurveySchedule.query.filter_by(
        captain_email=user.email
    ).first()

    return render_template(
        "captain/dashboard.html",
        schedule=schedule
    )


@captain_bp.route("/checklist", methods=["GET", "POST"])
def checklist():

    if session.get("role") not in [
    "captain",
    "backup_captain"
]:
     return redirect("/")

    user = User.query.get(session["user_id"])

    ongoing = Survey.query.filter(

    Survey.captain_email == user.email,

    Survey.status.in_([
        "ongoing",
        "groundwork_completed"
    ])

).first()

    if ongoing:
     flash(
        "You already have an ongoing survey. Please continue it before starting a new one.",
        "warning"
    )
     return redirect("/captain-home")

    if request.method == "POST":

        dashcam_photo = (
            request.files.get("dashcam_photo")
            or request.files.get("gallery_photo")
        )

        dashcam_url = None

        if dashcam_photo:

            import os

            from google_drive import upload_file_to_drive

            ext = os.path.splitext(
            dashcam_photo.filename
            )[1].lower()

            dashcam_name = (
            str(uuid.uuid4())
            + ext
            )

            import os
            import tempfile

            from google_drive import upload_file_to_drive

# Create temp file path
            fd, temp_path = tempfile.mkstemp(suffix=ext)
            os.close(fd)   # IMPORTANT: closes the file handle on Windows

# Save uploaded image
            dashcam_photo.save(temp_path)

# Compress image
            compressed_path = compress_image(temp_path)

# Read compressed image
            with open(compressed_path, "rb") as f:
              image_bytes = f.read()

# Upload to Google Drive
            result = upload_file_to_drive(
               file_bytes=image_bytes,
               filename=dashcam_name,
               folder_id="1W1SCf0_E28VdM8zfzzjFOR-aJL2-e0tT",
               mime_type="image/jpeg"
)

            dashcam_url = result["image_url"]

# Cleanup
            try:
             os.remove(temp_path)
            except Exception:
             pass

            try:
             os.remove(compressed_path)
            except Exception:
             pass

        assignment_id = session.get("assignment_id")

        if not assignment_id:
               return redirect("/captain-home")

        assignment = SurveyAssignment.query.get(assignment_id)
 
        if not assignment:
             return "Assignment not found"

        # -----------------------------------
        # WEEKLY DUPLICATE CHECK
        # -----------------------------------

        today = datetime.utcnow()

        days_since_sunday = (
            today.weekday() + 1
        ) % 7

        week_start = (
            today -
            timedelta(days=days_since_sunday)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        existing_survey = Survey.query.filter(
            Survey.section_no == assignment.section_no,
            Survey.start_time >= week_start,
            Survey.status.in_([
                "ongoing",
                "groundwork_completed",
                "video_pending",
                "completed"
            ])

        ).first()

        if (
            existing_survey and
            not existing_survey.resurvey_approved
        ):
            return f"""
            This survey has already been started.

            Captain : {existing_survey.captain_name}

            Status : {existing_survey.status}
            """

        # -----------------------------------
        # CYCLE LOGIC
        # -----------------------------------

        cycle_no = 1

        is_resurvey = session.get(
            "is_resurvey",
            False
        )

        latest_survey = Survey.query.filter(
            Survey.section_no == assignment.section_no
        ).order_by(
            Survey.id.desc()
        ).first()

        if latest_survey:
            cycle_no = latest_survey.cycle_no + 1

        # Existing admin-approved re-survey logic
        if not is_resurvey:

            approved_resurvey = Survey.query.filter(
                Survey.section_no == assignment.section_no,
                Survey.resurvey_approved == True
            ).order_by(
                Survey.id.desc()
            ).first()

            if approved_resurvey:

                cycle_no = approved_resurvey.cycle_no
                is_resurvey = True
                approved_resurvey.resurvey_approved = False

        # -----------------------------------
        # CREATE SURVEY
        # -----------------------------------

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
            survey_day=assignment.survey_day,
            survey_type=session["survey_type"],
            section_length=assignment.section_length,
            status="ongoing",
            start_time=datetime.now(
                pytz.timezone("Asia/Kolkata")
            ),
            dashcam_photo=dashcam_url,
            cycle_no=cycle_no,
            is_resurvey=is_resurvey
        )

        db.session.add(survey)

        if assignment.status == "missed":
          assignment.alert_acknowledged = False

        if session.get("role") == "backup_captain":
         assignment.status = "backup_in_progress"
        else:
         assignment.status = "started"

        db.session.commit()

        session.pop("is_resurvey", None)

        session["survey_id"] = survey.id

        return redirect("/recording")

    return render_template(
        "captain/checklist.html"
    )
        


@captain_bp.route("/recording")
def recording():

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    survey_id = session.get("survey_id")

    if survey_id:

        survey = Survey.query.get(survey_id)

    else:

        survey = Survey.query.filter(

         Survey.captain_email == user.email,

         Survey.status.in_([
    "ongoing",
    "groundwork_completed",
    "video_uploaded_pending_form"
])


        ).order_by(
            Survey.id.desc()
        ).first()

        if not survey:
            return redirect("/captain-home")

        session["survey_id"] = survey.id

    from datetime import timedelta

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

    if session.get("role") not in ["captain", "backup_captain"]:
        return redirect("/")

    survey = Survey.query.get(session.get("survey_id"))

    if not survey:
        return redirect("/captain-home")

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

        print(request.form)

        assignment_id = safe_int(
            request.form.get("assignment_id"),
            minimum=1
        )

        # A non-numeric assignment_id used to 500 on the bare int(). Only
        # accept one of *this* captain's own assignments - the id arrives from
        # a form the client controls, so trusting it would let any captain
        # start a survey against someone else's stretch.
        assignment = None

        if assignment_id is not None:
            assignment = SurveyAssignment.query.filter_by(
                id=assignment_id,
                captain_email=user.email
            ).first()

        if not assignment:
            flash("Please choose one of your assigned stretches.", "warning")
            return redirect("/select-stretch")

        session["assignment_id"] = assignment.id

        session["survey_day"] = request.form.get(
            "survey_day",
            assignment.survey_day
        )

        session["survey_type"] = request.form.get(
            "survey_type",
            assignment.survey_type
        )

        print(
            "ASSIGNMENT ID =",
            session["assignment_id"]
        )

        return redirect(
            "/survey-details"
        )
    
    print("TOTAL ASSIGNMENTS =", len(assignments))

    for a in assignments:
     print(a.id, a.stretch_code)
     

    return render_template(
        "captain/select_stretch.html",
        assignments=assignments,
        user=user
    )
   
    

@captain_bp.route("/survey-details")
def survey_details():

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    # This page only makes sense once /select-stretch (or the re-survey entry
    # point) has stashed a stretch in the session. Reaching it any other way -
    # a bookmark, the back button, or a session that expired mid-flow - used
    # to raise KeyError on session["assignment_id"] and return a 500. Send the
    # captain back to pick a stretch instead.
    assignment_id = session.get("assignment_id")

    if not assignment_id:
        return redirect("/select-stretch")

    assignment = SurveyAssignment.query.get(assignment_id)

    if not assignment:
        session.pop("assignment_id", None)
        return redirect("/select-stretch")

    return render_template(
        "captain/survey_details.html",
        assignment=assignment,
        survey_day=session.get("survey_day", assignment.survey_day),
        survey_type=session.get("survey_type", assignment.survey_type)
    )

@captain_bp.route(
    "/complete-survey",
    methods=["GET", "POST"]
)
def complete_survey():

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    if request.method == "POST":

        if request.form.get("verification_done"):

            survey = Survey.query.get(
                session.get("survey_id")
            )

            # The `if survey:` below used to sit *after* this status check, so
            # a stale or cleared survey_id - a double submit, a session that
            # outlived the survey - hit `None.status` and 500ed.
            if not survey:
                return redirect("/captain-home")

            if survey.status not in [
    "groundwork_completed",
    "video_uploaded_pending_form"
]:

             flash(
        "Please click 'Complete Groundwork' before finishing the survey.",
        "warning"
    )

             return redirect("/recording")

            if survey:

                pdf = request.files.get("survey_pdf")
                pdf.seek(0, os.SEEK_END)
                size = pdf.tell()
                pdf.seek(0)

                if size > 50 * 1024 * 1024:
                 flash("PDF size cannot exceed 50 MB.")
                 return redirect("/recording")

                if not pdf:

                    flash("Please upload the survey PDF.")

                    return redirect("/recording")

                # Allow only PDF files
                if (
                    pdf.mimetype != "application/pdf"
                    or not pdf.filename.lower().endswith(".pdf")
                ):

                    flash("Only PDF files are allowed.")

                    return redirect("/recording")

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

                if survey.video_uploaded:
                    survey.status = "completed"
                else:
                    survey.video_uploaded = False
                    survey.status = "video_pending"

                assignment = SurveyAssignment.query.filter_by(
                section_no=survey.section_no
                ).first()

                if assignment:

                  if session.get("role") == "backup_captain":

                   assignment.status = "completed_by_backup"

                  else:

                   assignment.status = "completed"

                survey.video_pending_start_time = (
                    datetime.utcnow()
                )

                db.session.commit()

                session.pop(
                    "survey_id",
                    None
                )

            if session.get("role") == "backup_captain":

                return redirect("/backup-home")

            else:

                 return redirect("/captain-home")

    if session.get("role") == "backup_captain":
      return redirect("/backup-home")

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

    user = User.query.get(
        session["user_id"]
    )

    ongoing_survey = Survey.query.filter(

    Survey.captain_email == user.email,

    Survey.status.in_([
        "ongoing",
        "groundwork_completed",
        "video_uploaded_pending_form"
    ])
).order_by(
    Survey.id.desc()
    ).first()

    assigned_count = SurveyAssignment.query.filter_by(
        captain_email=user.email
    ).count()

    pending_count = Survey.query.filter(

      Survey.captain_email == user.email,

      Survey.status.in_([
        "ongoing",
        "groundwork_completed",
        "video_uploaded_pending_form",
        "video_pending"
    ])

).count()

    completed_count = Survey.query.filter_by(
        captain_email=user.email,
        status="completed"
    ).count()

    return render_template(
        "captain/home.html",
        user=user,
        assigned_count=assigned_count,
        pending_count=pending_count,
        ongoing=ongoing_survey,
        completed_count=completed_count
    )



@captain_bp.route("/continue-survey")
def continue_survey():

    if session.get("role") not in ["captain", "backup_captain"]:
        return redirect("/")

    user = User.query.get(session["user_id"])

    survey = Survey.query.filter(

        Survey.captain_email == user.email,

        Survey.status.in_([
    "ongoing",
    "groundwork_completed",
    "video_uploaded_pending_form"
])

    ).order_by(
        Survey.id.desc()
    ).first()

    if not survey:
        flash("No ongoing survey found.", "warning")
        return redirect("/captain-home")

    session["survey_id"] = survey.id

    return redirect("/recording")


@captain_bp.route(
    "/video-counts/<int:survey_id>",
    methods=["GET", "POST"]
)
def video_counts(survey_id):

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    if request.method == "POST":

        # safe_count, not int(): a cleared number input posts "", which the
        # bare cast turned into a 500, and the template's min="0" is client-side
        # only, so a negative count used to be stored verbatim.
        survey.ir_lhs_count = safe_count(request.form.get("ir_lhs_count"))
        survey.mcw_lhs_count = safe_count(request.form.get("mcw_lhs_count"))
        survey.service_lhs_count = safe_count(request.form.get("service_lhs_count"))
        survey.slip_lhs_count = safe_count(request.form.get("slip_lhs_count"))

        survey.ir_rhs_count = safe_count(request.form.get("ir_rhs_count"))
        survey.mcw_rhs_count = safe_count(request.form.get("mcw_rhs_count"))
        survey.service_rhs_count = safe_count(request.form.get("service_rhs_count"))
        survey.slip_rhs_count = safe_count(request.form.get("slip_rhs_count"))

        survey.video_uploaded = True
        survey.video_upload_time = datetime.utcnow()

        if survey.status == "video_pending":
         survey.status = "completed"

        elif survey.status in [
         "ongoing",
         "groundwork_completed"
]:
         survey.status = "video_uploaded_pending_form"

        db.session.commit()

        return redirect("/captain-home")

    return render_template(
        "captain/video_counts.html",
        survey=survey
    )


@captain_bp.route("/pending-uploads")
def pending_uploads():

    if not session.get("user_id"):
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

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    survey = Survey.query.get_or_404(
        survey_id
    )

    video_status = request.form.get(
        "video_status"
    )

    if video_status == "yes":

        return redirect(
            f"/video-counts/{survey.id}"
        )

    return redirect(
        "/pending-uploads"
    )
@captain_bp.route("/completed-surveys")
def completed_surveys():

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    user = User.query.get(
        session["user_id"]
    )

    surveys = exclude_deleted(
        Survey.query.filter_by(
            captain_email=user.email,
            status="completed"
        ),
        Survey
    ).order_by(
        Survey.id.desc()
    ).all()

    return render_template(
        "captain/completed_surveys.html",
        surveys=surveys
    )


@captain_bp.route(
    "/completed-survey/<int:survey_id>"
)
def completed_survey_details(
    survey_id
):

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
        return redirect("/")

    survey = Survey.query.get_or_404(
        survey_id
    )

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

    if session.get("role") not in [
        "captain",
        "backup_captain"
    ]:
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

    # Scoped to the captain's own assignments, matching both the list this
    # link is rendered from (captain_resurvey, above) and /select-stretch.
    # An unscoped get_or_404 let any captain start a re-survey against another
    # captain's stretch just by editing the id in the URL.
    assignment = SurveyAssignment.query.filter_by(
        id=assignment_id,
        captain_email=user.email
    ).first_or_404()

    session["assignment_id"] = assignment.id

    session["survey_day"] = assignment.survey_day

    # survey_type carries a default of "Day", but rows predating that default
    # can still hold NULL, and None + str is a TypeError.
    session["survey_type"] = (
        (assignment.survey_type or "Day") +
        " Re-Survey"
    )

    session["is_resurvey"] = True

    return redirect(
        "/survey-details"
    )

@captain_bp.route("/backup-home")
def backup_home():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "backup_captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    # -----------------------------------
    # If Backup Captain already has an
    # ongoing survey, send him directly
    # to Recording page
    # -----------------------------------

    ongoing = Survey.query.filter(

    Survey.captain_email == user.email,

    Survey.status.in_([
    "ongoing",
    "groundwork_completed",
    "video_uploaded_pending_form"
])

    ).first()

    # -----------------------------------
    # Dashboard Counts
    # -----------------------------------

    available_count = SurveyAssignment.query.filter_by(
    state=user.state
    ).count()

    pending_count = Survey.query.filter(

    Survey.captain_email == user.email,

    Survey.status.in_([
        "ongoing",
        "groundwork_completed",
        "video_uploaded_pending_form",
        "video_pending"
    ])

).count()


    completed_count = Survey.query.filter_by(
    captain_email=user.email,
    status="completed"
    ).count()

    return render_template(
    "captain/backup_home.html",
    user=user,
    assigned_count=available_count,
    pending_count=pending_count,
    completed_count=completed_count,
    ongoing=ongoing
)

@captain_bp.route(
    "/backup-select-survey",
    methods=["GET", "POST"]
)
def backup_select_survey():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "backup_captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    assignments = SurveyAssignment.query.filter_by(
    state=user.state
    ).order_by(
    SurveyAssignment.stretch_code
    ).all()

    if request.method == "POST":

        # Same shape as /select-stretch: a non-numeric id used to 500 on the
        # bare int(). A backup captain picks from their whole state rather than
        # from assignments addressed to them, so scope the lookup that way.
        assignment_id = safe_int(
            request.form.get("assignment_id"),
            minimum=1
        )

        assignment = None

        if assignment_id is not None:
            assignment = SurveyAssignment.query.filter_by(
                id=assignment_id,
                state=user.state
            ).first()

        if not assignment:
            flash("Please choose a stretch from your state.", "warning")
            return redirect("/backup-select-survey")

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
        "captain/backup_select_survey.html",
        assignments=assignments,
        user=user
    )

@captain_bp.route("/backup-pending-uploads")
def backup_pending_uploads():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "backup_captain":
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

    ).all()

    return render_template(
        "captain/pending_uploads.html",
        surveys=surveys
    )


@captain_bp.route("/backup-completed-surveys")
def backup_completed_surveys():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "backup_captain":
        return redirect("/")

    user = User.query.get(session["user_id"])

    surveys = exclude_deleted(
        Survey.query.filter_by(
            captain_email=user.email,
            status="completed"
        ),
        Survey
    ).order_by(
        Survey.id.desc()
    ).all()

    return render_template(
        "captain/completed_surveys.html",
        surveys=surveys
    )



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
            return "Please select a PDF."

        if pdf.mimetype != "application/pdf":
            return "Only PDF files are allowed."

        # -----------------------------------
        # Delete Previous PDF (if exists)
        # -----------------------------------

        if survey.end_survey_pdf:

            try:

                old_file = survey.end_survey_pdf.split("/")[-1]

                supabase.storage.from_(
                    "survey-pdfs"
                ).remove([old_file])

            except Exception:
                pass

        # -----------------------------------
        # Upload New PDF
        # -----------------------------------

        filename = str(uuid.uuid4()) + ".pdf"

        supabase.storage.from_(
            "survey-pdfs"
        ).upload(
            filename,
            pdf.read(),
            {
                "content-type": "application/pdf"
            }
        )

        pdf_url = supabase.storage.from_(
            "survey-pdfs"
        ).get_public_url(filename)

        # -----------------------------------
        # Update Survey
        # -----------------------------------

        survey.end_survey_pdf = pdf_url

        # Clear re-upload request
        survey.pdf_reupload_required = False

        survey.pdf_reupload_reason = None

        db.session.commit()

        return redirect("/pending-uploads")

    return render_template(
        "captain/reupload_pdf.html",
        survey=survey
    )


