from flask import Blueprint
from flask import render_template
from flask import request
from extensions import db
from models.db_models import MissedSurveyHistory, SurveyAssignment
from models.db_models import User
from flask import session
import pytz
import logging
from sqlalchemy import case, func
from sqlalchemy.orm import defer
from models.db_models import Survey
from models.db_models import User
from flask import redirect
from datetime import datetime, timedelta
from google_drive import download_file_from_drive
from gemini_utils import extract_survey_dates_from_pdf, extract_survey_dates_from_drive
    

from utils.email_templates import (
    build_subject,
    build_email_body
)

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    jsonify,
    session,
    url_for,
    flash,
    current_app,
)

from google_drive import (
    PIU_EMAIL_MAPPING,
    download_file_from_drive,
    create_gmail_draft,
    get_cc_email,
    get_gmail,
)

from utils.request_params import (
    safe_date,
    safe_int,
    safe_week
)
from utils.defect_report_delay import (
    build_defect_email_index,
    find_defect_report_email,
    working_days_between,
    defect_report_delay_days,
)

admin_bp = Blueprint(
    "admin",
    __name__
)

log = logging.getLogger(__name__)


@admin_bp.route("/admin/delayed-surveys/manual/<int:survey_id>", methods=["POST"])
def manual_delayed_survey_update(survey_id):
    if session.get("role") != "admin":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)
    end_date_value = request.form.get("manual_end_date", "").strip()
    sent_at_value = request.form.get("manual_sent_at", "").strip()

    try:
        if end_date_value:
            survey.extracted_survey_end_date = datetime.strptime(
                end_date_value, "%Y-%m-%d"
            ).date()
            survey.survey_end_date_confidence = 1.0

        if sent_at_value:
            survey.defect_report_sent_at = datetime.fromisoformat(
                sent_at_value
            )
            survey.defect_report_sent_confidence = 1.0

        if not survey.extracted_survey_end_date or not survey.defect_report_sent_at:
            raise ValueError("Both manual dates are required.")

        survey.defect_report_delay_days = defect_report_delay_days(
            survey.extracted_survey_end_date,
            survey.defect_report_sent_at.date(),
        )
        survey.defect_report_match_status = "manual"
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.exception("Manual defect delay update failed for survey %s", survey_id)

    # Maintain the current week filter query parameter if it exists
    week_param = request.args.get("week")
    team_param = request.args.get("team")
    state_param = request.args.get("state")
    return redirect(url_for(
        "admin.delayed_surveys",
        week=week_param or None,
        team=team_param or None,
        state=state_param or None,
    ))


@admin_bp.route("/admin/delayed-surveys", methods=["GET", "POST"])
def delayed_surveys():
    if session.get("role") != "admin":
        return redirect("/")

    message = None

    # ============================================================
    # POST -> Sync Gmail data and calculate defect report delays
    # ============================================================
    if request.method == "POST":
        try:
            gmail = get_gmail()

            eligible_surveys = Survey.query.filter(
                Survey.survey_form_completed.is_(True),
                Survey.task1_completed.is_(True),
                Survey.task2_completed.is_(True),
                Survey.end_survey_pdf.isnot(None),
            ).all()

            email_index = build_defect_email_index(gmail)
            processed = 0

            for survey in eligible_surveys:
                survey.defect_report_match_status = "pending"

                try:
                    # ------------------------------------------------
                    # Extract survey end date from PDF if not already
                    # ------------------------------------------------
                    if not survey.extracted_survey_end_date:
                        dates = extract_survey_dates_from_drive(
                            survey.end_survey_pdf
                        )

                        survey.extracted_survey_end_date = datetime.strptime(
                            dates["end_date"],
                            "%Y-%m-%d"
                        ).date()

                        survey.survey_end_date_confidence = dates[
                            "end_confidence"
                        ]

                    # ------------------------------------------------
                    # Find defect report email
                    # ------------------------------------------------
                    match = find_defect_report_email(
                        survey,
                        email_index,
                        gmail
                    )

                    if not match:
                        survey.defect_report_match_status = "not_found"
                        survey.defect_report_sent_at = None
                        survey.defect_report_delay_days = None
                        continue

                    # ------------------------------------------------
                    # Save email information
                    # ------------------------------------------------
                    survey.defect_report_sent_at = match["sent_at"]
                    survey.defect_report_sent_confidence = 1.0
                    survey.defect_report_email_id = match["message_id"]

                    survey.defect_report_delay_days = defect_report_delay_days(
                        survey.extracted_survey_end_date,
                        match["sent_at"].date(),
                    )

                    survey.defect_report_match_status = "matched"

                except Exception as exc:
                    survey.defect_report_match_status = "error"

                    log.exception(
                        "Defect delay sync failed for survey %s: %s",
                        survey.id,
                        exc,
                    )

                processed += 1

                # Commit every 25 surveys
                if processed % 25 == 0:
                    db.session.commit()

                    log.info(
                        "Defect delay sync progress: %s/%s surveys",
                        processed,
                        len(eligible_surveys),
                    )

            # Final commit
            db.session.commit()

            message = (
                f"Delay data synchronized for {processed} eligible surveys. "
                f"Indexed {len(email_index)} sent messages."
            )

        except Exception as exc:
            db.session.rollback()

            message = (
                f"Gmail synchronization failed: {exc}"
            )

    # ============================================================
    # GET -> Display delayed surveys
    # ============================================================

    selected_week = safe_int(
        request.args.get("week")
    )
    selected_team = request.args.get("team", "").strip()
    selected_state = request.args.get("state", "").strip()
    team_states = {
        "Krish": ("WEST BENGAL", "ASSAM", "BIHAR", "MEGHALAYA"),
        "Godbole": ("ODISHA",),
        "Aspizo": ("UP", "UTTAR PRADESH", "JHARKHAND"),
    }

    # A survey appears only after the Team Leader ticks all three conditions
    # (Survey Form / Raw Video / Defect Report) as YES. That tick triggers the
    # auto-sync flow: Gemini extracts the survey end date -> Gmail API matches
    # the sent defect report mails -> the sent date is extracted -> the delay
    # is calculated. No other upload/tick state brings a record in here.
    from utils.visibility import exclude_deleted

    delayed_query = exclude_deleted(Survey.query, Survey).filter(
        Survey.survey_form_completed.is_(True),
        Survey.task1_completed.is_(True),
        Survey.task2_completed.is_(True),
        Survey.start_time >= datetime(2026, 8, 3),
        Survey.status.isnot(None),
        Survey.status != "cancelled",
    )

    if selected_team in team_states:
        delayed_query = delayed_query.filter(
            Survey.state.in_(team_states[selected_team])
        )
    if selected_state:
        state_values = (
            ("UP", "UTTAR PRADESH")
            if selected_state == "UP"
            else (selected_state,)
        )
        delayed_query = delayed_query.filter(Survey.state.in_(state_values))

    summary_surveys = delayed_query.all()

    # ------------------------------------------------------------
    # Week filter
    # ------------------------------------------------------------
    if selected_week is not None and selected_week >= 7:

        week_start = datetime(2026, 8, 3) + timedelta(
            days=(selected_week - 7) * 7
        )

        delayed_query = delayed_query.filter(
            Survey.start_time >= week_start,
            Survey.start_time < week_start + timedelta(days=7),
        )

    # ------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------
    delayed = delayed_query.order_by(
        Survey.defect_report_match_status.asc(),
        Survey.defect_report_delay_days.desc().nullslast(),
        Survey.extracted_survey_end_date.asc(),
    ).all()

    ist_offset = timedelta(hours=5, minutes=30)

    for survey in delayed:
        survey.display_start_time = (
            survey.start_time + ist_offset if survey.start_time else None
        )
        survey.display_end_time = (
            survey.end_time + ist_offset if survey.end_time else None
        )
        survey.display_pdf_upload_time = (
            survey.survey_pdf_uploaded_at + ist_offset
            if survey.survey_pdf_uploaded_at else None
        )
        survey.display_video_upload_time = (
            survey.video_upload_time + ist_offset
            if survey.video_upload_time else None
        )

    week_totals = {week: 0 for week in range(7, 53)}
    for survey in summary_surveys:
        if not survey.start_time or not survey.defect_report_delay_days:
            continue
        survey_week = 7 + (
            (survey.start_time.date() - datetime(2026, 8, 3).date()).days // 7
        )
        if survey_week in week_totals:
            week_totals[survey_week] += survey.defect_report_delay_days

    total_delay_days = sum(week_totals.values())

    # ============================================================
    # Render page
    # ============================================================

    return render_template(
        "admin/delayed_surveys.html",
        delayed_surveys=delayed,
        week_totals=week_totals or {},
        total_delay_days=total_delay_days or 0,
        selected_team=selected_team,
        selected_state=selected_state,
        message=message,
    )

from datetime import datetime

@admin_bp.route("/admin")
def admin_dashboard():
        
    if not session.get("user_id"):
     return redirect("/")

    if session.get("role") != "admin":
     return redirect("/")

    from datetime import datetime, timedelta
    # #region agent log
    import json as _dbg_json, time as _dbg_time
    _dbg_t0 = _dbg_time.perf_counter()
    def _dbg(hid, loc, msg, data=None):
        try:
            with open("debug-e17ea0.log", "a", encoding="utf-8") as _f:
                _f.write(_dbg_json.dumps({
                    "sessionId": "e17ea0",
                    "runId": "post-fix",
                    "hypothesisId": hid,
                    "location": loc,
                    "message": msg,
                    "data": data or {},
                    "timestamp": int(_dbg_time.time() * 1000),
                    "elapsed_ms": round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                }) + "\n")
        except Exception:
            pass
    _dbg("ALL", "admin.py:admin_dashboard:entry", "admin_dashboard started")
    # #endregion

    ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)

    today = ist_now.strftime("%A")
    current_hour = ist_now.hour

    days_since_monday = ist_now.weekday()

    week_start = (
    ist_now - timedelta(days=days_since_monday)
).replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0
)
    

# -----------------------------------
# WEEKLY RESET (Every Monday 12 AM onwards)
# -----------------------------------

    if today == "Monday":

     today_date = ist_now.date()

     reset_required = SurveyAssignment.query.filter(
        db.or_(
            SurveyAssignment.last_week_reset.is_(None),
            SurveyAssignment.last_week_reset < today_date
        )
    ).first()

     if reset_required:

        missed_assignments = SurveyAssignment.query.filter_by(
            status="missed"
        ).all()

        for assignment in missed_assignments:

            # =================================================
            # FIND LATEST SURVEY FOR THIS STRETCH
            # =================================================

            latest_survey = Survey.query.filter(
                Survey.section_no == assignment.section_no,
                Survey.upc_code == assignment.upc_code,
                Survey.stretch_code == assignment.stretch_code
            ).order_by(
                Survey.start_time.desc(),
                Survey.id.desc()
            ).first()

            # =================================================
            # CALCULATE MISSED CYCLE
            # =================================================

            if latest_survey and latest_survey.cycle_no is not None:
                missed_cycle = latest_survey.cycle_no + 1
            else:
                missed_cycle = 1

            # =================================================
            # PREVIOUS WEEK END = SUNDAY
            # =================================================

            previous_week_end = (
                week_start - timedelta(days=1)
            ).date()

            # =================================================
            # PREVENT DUPLICATE HISTORY
            # =================================================

            existing_history = MissedSurveyHistory.query.filter_by(
                section_no=str(assignment.section_no),
                cycle_no=missed_cycle,
                missed_date=previous_week_end
            ).first()

            if not existing_history:

                history = MissedSurveyHistory(
                    section_no=str(assignment.section_no),
                    cycle_no=missed_cycle,
                    upc_code=assignment.upc_code,
                    survey_day=assignment.survey_day,
                    main_person=assignment.main_person,
                    state=assignment.state,
                    missed_date=previous_week_end
                )

                db.session.add(history)

        # =====================================================
        # RESET ALL MISSED ASSIGNMENTS FOR NEW WEEK
        # =====================================================

        SurveyAssignment.query.filter(
            SurveyAssignment.status == "missed"
        ).update({
            "status": "pending",
            "captain_status": "pending",
            "captain_status_reason": None,
            "captain_status_updated_at": None,
            "alert_acknowledged": False,
            "missed_alert": False,
            "missed_reason": None,
            "last_week_reset": week_start.date()
        }, synchronize_session=False)

        db.session.commit()
            

        # This is a bulk SQL update, so the API's change-capture listeners never
        # see it and the consuming site would silently miss the whole reset.
        # Announce it explicitly. Deliberately after the commit, and best-effort:
        # the Monday reset must not fail because the feed is unavailable.
        try:
            from core.engine import record_bulk_change

            record_bulk_change(
                "assignment",
                reason="weekly Monday reset (routes/admin.py)"
            )
        except Exception:
            pass

    # #region agent log
    _dbg("F", "admin.py:after_monday_reset", "monday reset block finished")
    # #endregion
    

    total_captains = User.query.filter_by(
        role="captain"
    ).count()

    status_totals = dict(
        db.session.query(
            Survey.status,
            func.count(Survey.id),
        )
        .group_by(Survey.status)
        .all()
    )
    ongoing = status_totals.get("ongoing", 0)
    completed = status_totals.get("completed", 0)
    video_pending = status_totals.get("video_pending", 0)

    ist_offset = timedelta(hours=5, minutes=30)
    utc_now = datetime.utcnow()
    now_ist = utc_now + ist_offset

    status = request.args.get("status")
    pdf_delayed = status == "pdf_delayed"
    video_delayed = status == "video_delayed"
    stretch = request.args.get("stretch")
    state = request.args.get("state")
    captain_name = request.args.get("captain_name")
    cycle = request.args.get("cycle")
    week = request.args.get("week")
    day = request.args.get("day")
    agency = request.args.get("agency", "")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    video_upload_date = request.args.get("video_upload_date")

    other_filters_applied = any([
     status,
     stretch,
     state,
     captain_name,
     cycle,
     day,
     agency,
     from_date,
     to_date,
    video_upload_date,
])
    

    query = Survey.query.filter(
    Survey.show_on_dashboard.is_(True)
).filter(
    db.or_(
        Survey.status != "pending",
        Survey.captain_status.in_([
            "rescheduled",
            "cancelled"
        ])
    )
)
   

    roadvision_pending_query = Survey.query.filter(
    Survey.status == "completed",
    Survey.roadvision_completed.is_(False)
)

    
    if status:

     if status == "pdf_reupload_required":

        query = query.filter(
            Survey.pdf_reupload_required.is_(True)
        )

     elif status == "pdf_delayed":

        query = query.filter(
            Survey.end_time.isnot(None),
            Survey.survey_pdf_uploaded_at.is_(None)
        )

     elif status == "video_delayed":

        query = query.filter(
            Survey.end_time.isnot(None),
            Survey.video_upload_time.is_(None)
        )

     elif status == "rescheduled":

        query = query.filter(
            Survey.status == "rescheduled"
        )

     elif status == "cancelled":

        query = query.filter(
            Survey.status == "cancelled"
        )

     else:

        query = query.filter_by(
            status=status
        )

    if stretch:
        query = query.filter(
            Survey.stretch_code.ilike(
                f"%{stretch}%"
            )
        )

    if state:
        query = query.filter_by(state=state)

    if agency == "krish":

     query = query.filter(
        Survey.state.in_([
            "MEGHALAYA",
            "WEST BENGAL",
            "ASSAM",
            "BIHAR"
        ])
    )

    elif agency == "godbole":
 
     query = query.filter(
        Survey.state == "ODISHA"
    )

    elif agency == "aspizo":

     query = query.filter(
        Survey.state.in_([
            "UTTAR PRADESH",
            "JHARKHAND"
        ])
    )

    if captain_name:
        query = query.filter_by(
            captain_name=captain_name
        )

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        query = query.filter_by(
        cycle_no=cycle_no
    )

    # `day` filters both queries, but only when one was actually chosen. The
    # roadvision line used to sit outside this block, so with no day filter -
    # the default view - it ran as filter_by(survey_day=None) and matched only
    # rows with a NULL survey_day. The RoadVision-pending counter therefore
    # read 0 until an admin picked a day.
    if day:
        query = query.filter_by(
        survey_day=day
    )

        roadvision_pending_query = roadvision_pending_query.filter_by(
            survey_day=day
        )

# -----------------------------------
# PROJECT WEEK FILTER
# -----------------------------------

    project_start = datetime(2026, 6, 22)
    current_week_no = (
    (datetime.utcnow().date() - project_start.date()).days // 7
) + 1

    if week:
     week_no = safe_week(week)

    elif not other_filters_applied:
     week_no = current_week_no

    else:
       week_no= None

    if week_no is not None:
        
       start = project_start + timedelta(
        days=(week_no - 1) * 7
    )

       end = start + timedelta(days=7)

       query = query.filter(
        Survey.start_time >= start,
        Survey.start_time < end
    )

       roadvision_pending_query = roadvision_pending_query.filter(
        Survey.start_time >= start,
        Survey.start_time < end
    )


    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    start_dt = safe_date(start_date)
    end_dt = safe_date(end_date)
    video_upload_dt = safe_date(video_upload_date)

    # Date inputs represent the displayed IST calendar date, while timestamps
    # are stored as UTC-naive values in the database.
    start_dt_utc = start_dt - ist_offset if start_dt else None
    end_dt_utc = end_dt - ist_offset if end_dt else None
    video_upload_dt_utc = (
        video_upload_dt - ist_offset
        if video_upload_dt else None
    )

# Start Date → survey STARTED on this date
    if start_dt_utc:
     query = query.filter(
        Survey.start_time >= start_dt_utc,
        Survey.start_time < start_dt_utc + timedelta(days=1)
    )

# End Date → survey ENDED on this date
    if end_dt_utc:
     query = query.filter(
        Survey.end_time >= end_dt_utc,
        Survey.end_time < end_dt_utc + timedelta(days=1)
    )

# Video Upload Date → video uploaded on this displayed IST date
    if video_upload_dt_utc:
     query = query.filter(
        Survey.video_upload_time >= video_upload_dt_utc,
        Survey.video_upload_time < video_upload_dt_utc + timedelta(days=1)
    )

    status_count_query = Survey.query.filter(
    Survey.show_on_dashboard.is_(True)
).filter(
    Survey.status != "pending"
)

# STATE
    if state:
     status_count_query = status_count_query.filter(
        Survey.state == state
    )

# TEAM / AGENCY
    if agency == "krish":

     status_count_query = status_count_query.filter(
        Survey.state.in_([
            "MEGHALAYA",
            "WEST BENGAL",
            "ASSAM",
            "BIHAR"
        ])
    )

    elif agency == "godbole":

     status_count_query = status_count_query.filter(
        Survey.state == "ODISHA"
    )

    elif agency == "aspizo":

     status_count_query = status_count_query.filter(
        Survey.state.in_([
            "UTTAR PRADESH",
            "JHARKHAND"
        ])
    )

# CAPTAIN
    if captain_name:
     status_count_query = status_count_query.filter(
        Survey.captain_name == captain_name
    )

# CYCLE
    if cycle_no is not None:
     status_count_query = status_count_query.filter(
        Survey.cycle_no == cycle_no
    )

# DAY
    if day:
     status_count_query = status_count_query.filter(
        Survey.survey_day == day
    )

# WEEK
    if week_no is not None:

     count_week_start = project_start + timedelta(
        days=(week_no - 1) * 7
    )

    count_week_end = count_week_start + timedelta(days=7)

    status_count_query = status_count_query.filter(
        Survey.start_time >= count_week_start,
        Survey.start_time < count_week_end
    )

# DATE RANGE
    if start_dt_utc:
         query = query.filter(
            Survey.start_time >= start_dt_utc,
            Survey.start_time < start_dt_utc + timedelta(days=1)
        )

    if end_dt_utc:
         query = query.filter(
            Survey.end_time >= end_dt_utc,
            Survey.end_time < end_dt_utc + timedelta(days=1)
        )

    pdf_reupload_count = 0
    _status_counts = {}
    for _st, _pdf_flag, _n in (
        status_count_query
        .with_entities(
            Survey.status,
            Survey.pdf_reupload_required,
            func.count(Survey.id),
        )
        .group_by(Survey.status, Survey.pdf_reupload_required)
        .all()
    ):
        if _pdf_flag is True:
            pdf_reupload_count += _n
        elif _pdf_flag is False:
            _status_counts[_st] = _status_counts.get(_st, 0) + _n

    groundwork_completed_count = _status_counts.get("groundwork_completed", 0)
    video_uploaded_pending_form_count = _status_counts.get(
        "video_uploaded_pending_form", 0
    )
    video_pending_count = _status_counts.get("video_pending", 0)
    ongoing_count = _status_counts.get("ongoing", 0)
    rescheduled_count = _status_counts.get("rescheduled", 0)
    cancelled_count = _status_counts.get("cancelled", 0)
    completed_count = _status_counts.get("completed", 0)

    # #region agent log
    _dbg("B", "admin.py:after_status_counts", "status count queries finished", {
        "pdf_reupload_count": pdf_reupload_count,
        "completed_count": completed_count,
        "ongoing_count": ongoing_count,
    })
    # #endregion

    assignment_rows = (
        SurveyAssignment.query
        .order_by(SurveyAssignment.id.desc())
        .all()
    )
    states = [
        (state,)
        for state in sorted({
            assignment.state
            for assignment in assignment_rows
            if assignment.state is not None
        })
    ]
    
    captains = User.query.filter_by(
        role="captain"
    ).order_by(
        User.name
    ).all()

# -----------------------------
# PROJECT WEEK DROPDOWN
# -----------------------------

    project_start = datetime(2026, 6, 22)

    today_date = datetime.utcnow()

    total_weeks = (
        (today_date.date() - project_start.date()).days // 7
         ) + 1

    weeks = list(range(1, total_weeks + 1))

    from sqlalchemy import case, and_, or_

    _heavy_survey_cols = (
        Survey.end_survey_pdf,
        Survey.end_survey_photo,
        Survey.dashcam_photo,
        Survey.settings_photo,
        Survey.defect_report_file,
        Survey.raw_video_excel_file,
    )

    all_surveys = query.options(
        *(defer(col) for col in _heavy_survey_cols)
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
            Survey.status == "rescheduled",
            6
        ),

        # 7. Cancelled
        (
            Survey.status == "cancelled",
            7
        ),

        # 8. Completed but some tasks are still pending
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

    if status in ("pdf_delayed", "video_delayed"):
        def deadline_passed(survey):
            end_time_ist = (
                survey.end_time + ist_offset
            ).replace(tzinfo=None)
            deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time(),
            ) + timedelta(hours=14)
            return now_ist > deadline_ist

        all_surveys = [
            survey for survey in all_surveys
            if deadline_passed(survey)
        ]

    # #region agent log
    _dbg("C", "admin.py:after_all_surveys", "main survey list loaded", {
        "all_surveys_count": len(all_surveys),
    })
    # #endregion

    cycles = [
        (cycle,)
        for cycle in sorted(
            {survey.cycle_no for survey in all_surveys},
            key=lambda value: (value is None, value or 0),
        )
    ]

    extract_groups = {
    "PDF Re-upload Required": [],
    "Groundwork Completed - Video and Form Pending": [],
    "Video Pending": [],
    "Ongoing": [],
    "Rescheduled": [],
    "Cancelled": [],
    "Completed": []
}

    assignment_by_upc = {}
    assignment_by_section_state = {}
    for assignment in assignment_rows:
        if assignment.upc_code and assignment.upc_code not in assignment_by_upc:
            assignment_by_upc[assignment.upc_code] = assignment
        section_state_key = (assignment.section_no, assignment.state)
        if section_state_key not in assignment_by_section_state:
            assignment_by_section_state[section_state_key] = assignment

    current_state_km = {}

    for survey in all_surveys:
        extract_key = f"{survey.upc_code}_Cycle{survey.cycle_no}"
        if survey.pdf_reupload_required:
            extract_groups["PDF Re-upload Required"].append(extract_key)
        elif survey.status == "groundwork_completed":
            extract_groups[
                "Groundwork Completed - Video and Form Pending"
            ].append(extract_key)
        elif survey.status == "video_pending":
            extract_groups["Video Pending"].append(extract_key)
        elif survey.status == "ongoing":
            extract_groups["Ongoing"].append(extract_key)
        elif survey.status == "rescheduled":
            extract_groups["Rescheduled"].append(extract_key)
        elif survey.status == "cancelled":
            extract_groups["Cancelled"].append(extract_key)
        elif survey.status == "completed":
            extract_groups["Completed"].append(extract_key)

        assignment = None
        if survey.upc_code:
            assignment = assignment_by_upc.get(survey.upc_code)
        if not assignment:
            assignment = assignment_by_section_state.get(
                (survey.section_no, survey.state)
            )
        survey.captain_display_reason = (
            (assignment.captain_status_reason or "").strip()
            if assignment else None
        )

        if (
            survey.task1_completed is True
            and survey.task2_completed is True
            and survey.survey_form_completed is True
            and survey.state
        ):
            current_state_km[survey.state] = (
                current_state_km.get(survey.state, 0)
                + (survey.section_length or 0)
            )

        survey.scheduled_day = survey.survey_day

        if (
            survey.captain_status in ["rescheduled", "cancelled"]
            and survey.captain_status_updated_at
        ):
            survey.display_start_time = (
                survey.captain_status_updated_at + ist_offset
            )
        elif survey.start_time:
            survey.display_start_time = survey.start_time
        else:
            survey.display_start_time = None

        if survey.captain_status in ["rescheduled", "cancelled"]:
            survey.display_end_time = None
        elif survey.end_time:
            survey.display_end_time = survey.end_time + ist_offset
        else:
            survey.display_end_time = None

        if survey.task1_completed and survey.task1_completed_at:
            survey.task1_completed_at_display = (
                survey.task1_completed_at + ist_offset
            )
        else:
            survey.task1_completed_at_display = None

        if survey.task2_completed and survey.task2_completed_at:
            survey.task2_completed_at_display = (
                survey.task2_completed_at + ist_offset
            )
        else:
            survey.task2_completed_at_display = None

        if survey.video_upload_time:
            survey.display_video_upload_time = (
                survey.video_upload_time + ist_offset
            )
        else:
            survey.display_video_upload_time = None

        if survey.survey_pdf_uploaded_at:
            survey.display_pdf_upload_time = (
                survey.survey_pdf_uploaded_at + ist_offset
            )
        else:
            survey.display_pdf_upload_time = None

        survey.pdf_upload_late = False

        if survey.end_time:

            # Survey end time → IST
            end_time_ist = (
                survey.end_time + ist_offset
            ).replace(tzinfo=None)

            # Deadline → next day 2:00 PM IST
            pdf_deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time()
            ) + timedelta(hours=14)

            # If PDF is already uploaded
            if survey.survey_pdf_uploaded_at:

                pdf_upload_time_ist = (
                    survey.survey_pdf_uploaded_at + ist_offset
                ).replace(tzinfo=None)

                # Uploaded after 2 PM → delayed
                if pdf_upload_time_ist > pdf_deadline_ist:
                    survey.pdf_upload_late = True

            # If PDF is NOT uploaded yet
            else:

                now_ist = (utc_now + ist_offset).replace(tzinfo=None)

                # Deadline passed → delayed
                if now_ist > pdf_deadline_ist:
                    survey.pdf_upload_late = True

        survey.video_upload_late = False

        if survey.end_time:

            # Survey end time → IST
            end_time_ist = (
                survey.end_time + ist_offset
            ).replace(tzinfo=None)

            # Deadline → next day 2:00 PM IST
            video_deadline_ist = datetime.combine(
                end_time_ist.date() + timedelta(days=1),
                datetime.min.time()
            ) + timedelta(hours=14)

            # Video already uploaded
            if survey.video_upload_time:

                video_upload_time_ist = (
                    survey.video_upload_time + ist_offset
                ).replace(tzinfo=None)

                # Uploaded after deadline → delayed
                if video_upload_time_ist > video_deadline_ist:
                    survey.video_upload_late = True

            # Video NOT uploaded yet
            else:

                now_ist = (utc_now + ist_offset).replace(tzinfo=None)

                # Deadline passed → delayed
                if now_ist > video_deadline_ist:
                    survey.video_upload_late = True

        if (
            survey.status == "video_pending"
            and survey.video_pending_start_time
        ):
            survey.upload_duration_minutes = int(
                (utc_now - survey.video_pending_start_time).total_seconds() / 60
            )
            survey.upload_status_text = "Upload Pending"
        elif survey.video_pending_start_time and survey.video_upload_time:
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

    # #region agent log
    _dbg("A", "admin.py:after_assignment_nplus1", "per-survey assignment lookups finished", {
        "all_surveys_count": len(all_surveys),
        "assignment_rows_count": len(assignment_rows),
    })
    # #endregion

    overall_km_rows = (
        db.session.query(
            Survey.state,
            func.coalesce(func.sum(Survey.section_length), 0),
        )
        .filter(
            Survey.show_on_dashboard.is_(True),
            Survey.task1_completed.is_(True),
            Survey.task2_completed.is_(True),
            Survey.survey_form_completed.is_(True),
            Survey.state.isnot(None),
        )
        .group_by(Survey.state)
        .all()
    )
    overall_state_km = {
        state_name: float(km or 0) for state_name, km in overall_km_rows
    }

    total_km = sum(current_state_km.values())
    overall_total_km = sum(overall_state_km.values())

    # #region agent log
    _dbg("E", "admin.py:after_km_loops", "km aggregation finished", {
        "overall_completed_surveys_count": len(overall_km_rows),
        "current_state_count": len(current_state_km),
    })
    # #endregion


# -----------------------------------
# RESURVEY REQUESTS
# -----------------------------------

    resurvey_requests = Survey.query.options(
        *(defer(col) for col in _heavy_survey_cols)
    ).filter_by(
    resurvey_requested=True,
    resurvey_approved=False
).all()

    # #region agent log
    _dbg("C", "admin.py:after_display_loops", "display-time loops finished", {
        "all_surveys_count": len(all_surveys),
    })
    # #endregion

    # -----------------------------------
    # MISSED SURVEY LOGIC
    # -----------------------------------

    alerts = []

    today_assignments = [
        assignment
        for assignment in assignment_rows
        if assignment.survey_day == today and assignment.survey_enabled
    ]

    missed_changed = False

    if current_hour >= 13:

        current_week_end = week_start + timedelta(days=7)

        started_keys = set()
        if today_assignments:
            started_keys = set(
                db.session.query(
                    Survey.captain_email,
                    Survey.section_no,
                    Survey.stretch_code,
                    Survey.upc_code,
                ).filter(
                    Survey.start_time >= week_start,
                    Survey.start_time < current_week_end,
                    Survey.status.in_([
                        "cancelled",
                        "ongoing",
                        "rescheduled",
                        "video_uploaded_pending_form",
                        "groundwork_completed",
                        "video_pending",
                        "completed",
                    ]),
                ).all()
            )

        for assignment in today_assignments:

            if assignment.status == "missed":

                if not assignment.alert_acknowledged:

                    alerts.append({
                        "assignment_id": assignment.id,
                        "captain": assignment.main_person,
                        "message": "Survey not started by 1 PM",
                        "state": assignment.state,
                        "stretch": assignment.stretch_code,
                        "section_no": assignment.section_no
                    })

                continue

            survey_exists = (
                assignment.captain_email,
                assignment.section_no,
                assignment.stretch_code,
                assignment.upc_code,
            ) in started_keys

            if not survey_exists:

                assignment.status = "missed"
                missed_changed = True


    # =========================================
    # TOTAL MISSED
    # =========================================

    missed = sum(
        1 for assignment in assignment_rows
        if assignment.status == "missed"
    )


    roadvision_pending = roadvision_pending_query.count()

    if missed_changed:
        db.session.commit()

    # #region agent log
    _dbg("D", "admin.py:after_missed_logic", "missed-survey N+1 and commit finished", {
        "today_assignments_count": len(today_assignments),
        "alerts_count": len(alerts),
        "missed": missed,
        "current_hour": current_hour,
    })
    # #endregion

    _html = render_template(
    "admin/dashboard.html",
    total_captains=total_captains,
    ongoing=ongoing,
    completed=completed,
    video_pending=video_pending,
    missed=missed,
    all_surveys=all_surveys,
    states=states,
    captains=captains,
    alerts=alerts,
    cycles=cycles,
    weeks=weeks,
    agency=agency,
    extract_groups=extract_groups,
    timedelta=timedelta,
    ongoing_count=ongoing_count,
    completed_count=completed_count,
    video_uploaded_pending_form_count=video_uploaded_pending_form_count,
    video_pending_count=video_pending_count,
    groundwork_completed_count=groundwork_completed_count,
    pdf_reupload_count=pdf_reupload_count,
    rescheduled_count=rescheduled_count,
    cancelled_count=cancelled_count,
    current_week_no=current_week_no,
    resurvey_requests=resurvey_requests,
    roadvision_pending=roadvision_pending,
    current_state_km=current_state_km,
    overall_state_km=overall_state_km,
    total_km=round(total_km, 2),
    overall_total_km=round(overall_total_km, 2)
    )
    # #region agent log
    _dbg("ALL", "admin.py:admin_dashboard:exit", "admin_dashboard finished including template", {
        "html_len": len(_html) if _html else 0,
    })
    # #endregion
    return _html


@admin_bp.route("/admin/missed")
def admin_missed():

    if session.get("role") != "admin":
        return redirect("/")

    agency = request.args.get("agency")
    day = request.args.get("day")
    day_from = request.args.get("day_from")
    day_to = request.args.get("day_to")

    # =========================================================
    # MISSED ASSIGNMENTS
    # =========================================================

    query = SurveyAssignment.query.filter_by(
        status="missed"
    )

    # =========================================================
    # AGENCY FILTER
    # =========================================================

    if agency == "krish":

        query = query.filter(
            SurveyAssignment.state.in_([
                "ASSAM",
                "BIHAR",
                "MEGHALAYA",
                "WEST BENGAL"
            ])
        )

    elif agency == "godbole":

        query = query.filter(
            SurveyAssignment.state == "ODISHA"
        )

    elif agency == "aspizo":

        query = query.filter(
            SurveyAssignment.state.in_([
                "UTTAR PRADESH",
                "JHARKHAND"
            ])
        )

    # =========================================================
    # SINGLE DAY FILTER
    # =========================================================

    if day:

        query = query.filter(
            SurveyAssignment.survey_day == day
        )

    # =========================================================
    # DAY RANGE FILTER
    # =========================================================

    days_order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday"
    ]

    if day_from and day_to:

        start_index = days_order.index(day_from)
        end_index = days_order.index(day_to)

        if start_index <= end_index:

            selected_days = days_order[
                start_index:end_index + 1
            ]

        else:

            selected_days = (
                days_order[start_index:]
                + days_order[:end_index + 1]
            )

        query = query.filter(
            SurveyAssignment.survey_day.in_(selected_days)
        )

    # =========================================================
    # GET MISSED SURVEYS
    # =========================================================

    missed_surveys = query.order_by(
        SurveyAssignment.survey_day,
        SurveyAssignment.section_no
    ).all()

    # =========================================================
    # CURRENT WEEK
    # =========================================================

    from datetime import datetime, timedelta

    today = datetime.utcnow().date()

    current_week_start = (
        today - timedelta(days=today.weekday())
    )

    # =========================================================
    # MISSED HISTORY
    # =========================================================

    missed_history = MissedSurveyHistory.query.filter(
        MissedSurveyHistory.missed_date < current_week_start
    ).order_by(
        MissedSurveyHistory.missed_date.desc(),
        MissedSurveyHistory.section_no
    ).all()

    # =========================================================
    # BUILD EXTRACT LIST
    #
    # IMPORTANT:
    # SurveyAssignment.cycle_no is NOT used here.
    #
    # Logic:
    #
    # 1. Find an actual previous Survey for THIS stretch.
    # 2. If one exists:
    #       next cycle = previous cycle + 1
    #
    # 3. If no Survey exists:
    #       cycle = 1
    #
    # Therefore an assignment having cycle_no=1 cannot
    # accidentally make a brand-new survey become cycle 2.
    # =========================================================

    missed_extract_list = []
    missed_cycle_map = {}

    for assignment in missed_surveys:

        previous_survey = Survey.query.filter(
            Survey.section_no == assignment.section_no,
            Survey.stretch_code == assignment.stretch_code,
            Survey.upc_code == assignment.upc_code
        ).order_by(
            Survey.start_time.desc(),
            Survey.id.desc()
        ).first()

        # -----------------------------------------------------
        # EXISTING SURVEY FOR THIS STRETCH
        # -----------------------------------------------------

        if (
            previous_survey
            and previous_survey.cycle_no is not None
        ):

            if previous_survey.status in[
               "cancelled",
               "rescheduled"
            ]:

               next_cycle = previous_survey.cycle_no

            else:

               next_cycle = previous_survey.cycle_no + 1

        else:

            next_cycle = 1

        missed_extract_list.append(
            f"{assignment.section_no}_Cycle{next_cycle}"
        )

        missed_cycle_map[
            str(assignment.section_no)
        ] = next_cycle

    # =========================================================
    # RENDER
    # =========================================================

    return render_template(
        "admin/missed.html",

        missed_surveys=missed_surveys,

        missed_history=missed_history,

        missed_extract_list=missed_extract_list,

        missed_cycle_map=missed_cycle_map
    )

@admin_bp.route("/admin/missed/extract")
def missed_extract():

    if session.get("role") != "admin":
        return redirect("/")

    agency = request.args.get("agency")
    day = request.args.get("day")

    query = SurveyAssignment.query.filter_by(
        status="missed"
    )

    if agency == "krish":
     query = query.filter(
        SurveyAssignment.state.in_([
            "ASSAM",
            "BIHAR",
            "MEGHALAYA",
            "WEST BENGAL"
        ])
    )

    elif agency == "godbole":
     query = query.filter(
        SurveyAssignment.state == "ODISHA"
    )

    elif agency == "aspizo":
     query = query.filter(
        SurveyAssignment.state.in_([
            "UTTAR PRADESH",
            "JHARKHAND"
        ])
    )

    if day:
        query = query.filter(
            SurveyAssignment.survey_day == day
        )

    missed_surveys = query.order_by(
        SurveyAssignment.section_no
    ).all()

    missed_list = []

    for assignment in missed_surveys:

        latest_survey = Survey.query.filter_by(
            section_no=assignment.section_no
        ).order_by(
            Survey.cycle_no.desc()
        ).first()

        if latest_survey:
            next_cycle = latest_survey.cycle_no + 1
        else:
            next_cycle = 1

        missed_list.append({
            "section_no": assignment.section_no,
            "cycle_no": next_cycle
        })

    return render_template(
        "admin/missed_extract.html",
        missed_list=missed_list
    )

@admin_bp.route("/admin/survey/<int:survey_id>")
def survey_details_admin(survey_id):

    # Allow Admin + Regional Manager
    if session.get("role") not in ["admin", "regional_manager"]:
        return redirect("/")

    from datetime import timedelta

    survey = Survey.query.get_or_404(survey_id)

    display_start_time = None
    display_end_time = None

    if survey.start_time:
        display_start_time = (
            survey.start_time +
            timedelta(hours=5, minutes=30)
        )

    if survey.end_time:
        display_end_time = (
            survey.end_time +
            timedelta(hours=5, minutes=30)
        )

    return render_template(
        "admin/survey_details.html",
        survey=survey,
        display_start_time=display_start_time,
        display_end_time=display_end_time
    )


@admin_bp.route(
    "/approve-resurvey/<int:survey_id>"
)
def approve_resurvey(survey_id):

    if session.get("role") != "admin":
        return redirect("/")

    survey = Survey.query.get_or_404(
        survey_id
    )

    survey.resurvey_approved = True

    db.session.commit()

    return redirect("/admin")


@admin_bp.route("/admin/schedules")
def admin_schedules():

    if session.get("role") != "admin":
        return redirect("/")

    day = request.args.get("day")
    state = request.args.get("state")

    query = SurveyAssignment.query

    if day:
        query = query.filter_by(
            survey_day=day
        )

    if state:
        query = query.filter_by(
            state=state
        )

    from sqlalchemy import case

    day_order = case(
    (SurveyAssignment.survey_day == "Monday", 1),
    (SurveyAssignment.survey_day == "Tuesday", 2),
    (SurveyAssignment.survey_day == "Wednesday", 3),
    (SurveyAssignment.survey_day == "Thursday", 4),
    (SurveyAssignment.survey_day == "Friday", 5),
    else_=6
)

    schedules = query.order_by(
    day_order,
    SurveyAssignment.state,
    SurveyAssignment.section_no
).all()

    states = SurveyAssignment.query.with_entities(
        SurveyAssignment.state
    ).distinct().all()

    monday_count = SurveyAssignment.query.filter_by(
        survey_day="Monday"
    ).count()

    tuesday_count = SurveyAssignment.query.filter_by(
        survey_day="Tuesday"
    ).count()

    wednesday_count = SurveyAssignment.query.filter_by(
        survey_day="Wednesday"
    ).count()

    thursday_count = SurveyAssignment.query.filter_by(
        survey_day="Thursday"
    ).count()

    friday_count = SurveyAssignment.query.filter_by(
        survey_day="Friday"
    ).count()

    return render_template(
        "admin/schedules.html",
        schedules=schedules,
        states=states,
        monday_count=monday_count,
        tuesday_count=tuesday_count,
        wednesday_count=wednesday_count,
        thursday_count=thursday_count,
        friday_count=friday_count
    )


@admin_bp.route(
    "/request-pdf-reupload/<int:survey_id>",
    methods=["POST"]
)
def request_pdf_reupload(survey_id):

    if session.get("role") != "admin":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    survey.pdf_reupload_required = True
    survey.survey_pdf_uploaded_at = None

    survey.pdf_reupload_reason = request.form["reason"]

    survey.pdf_reupload_count += 1

    db.session.commit()

    return redirect(f"/admin/survey/{survey.id}")


@admin_bp.route("/reports")
def reports():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "admin":
        return redirect("/")

    from datetime import datetime, timedelta

    query = Survey.query.filter(
     Survey.task1_completed.is_(True),
     Survey.task2_completed.is_(True),
     Survey.survey_form_completed.is_(True)
)

    # --------------------------
    # FILTERS
    # --------------------------

    week = request.args.get("week")
    cycle = request.args.get("cycle")
    team = request.args.get("team")
    state = request.args.get("state")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    # Cycle

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        query = query.filter_by(
            cycle_no=cycle_no
        )

    # Week

    project_start = datetime(2026, 6, 22)

    week_no = safe_week(week)

    if week_no is not None:

        start = project_start + timedelta(
            days=(week_no - 1) * 7
        )

        end = start + timedelta(days=7)

        query = query.filter(
            Survey.start_time >= start,
            Survey.start_time < end
        )

    # Date

    from_dt = safe_date(from_date)
    to_dt = safe_date(to_date)

    if from_dt:

        query = query.filter(
            Survey.start_time >= from_dt
        )

    if to_dt:

        query = query.filter(
            Survey.start_time <
            to_dt + timedelta(days=1)
        )

    # Team

    if team == "Krish":

        query = query.filter(
            Survey.state.in_([
                "WEST BENGAL",
                "ASSAM",
                "BIHAR"
            ])
        )

    elif team == "Godbole":

        query = query.filter(
            Survey.state == "ODISHA"
        )

    elif team == "Aspizo":

        query = query.filter(
            Survey.state.in_([
                "UTTAR PRADESH",
                "JHARKHAND"
            ])
        )

    # State

    if state:
         query = query.filter(
        Survey.state == state
    )

    surveys = query.all()

    # --------------------------
    # SUMMARY
    # --------------------------

    completed_surveys = len(surveys)

    total_km = sum(
        s.section_length or 0
        for s in surveys
    )

    captains = len(set(
        s.captain_email
        for s in surveys
    ))

    total_minutes = 0

    for survey in surveys:

        if survey.start_time and survey.end_time:

            total_minutes += (
                survey.end_time -
                survey.start_time
            ).total_seconds() / 60

    total_hours = round(
        total_minutes / 60,
        2
    )

    # --------------------------
    # FILTERS
    # --------------------------

    today = datetime.utcnow()

    total_weeks = (
        (today.date()-project_start.date()).days//7
    ) + 1

    weeks = list(
        range(
            1,
            total_weeks+1
        )
    )

    cycles = db.session.query(
        Survey.cycle_no
    ).distinct().order_by(
        Survey.cycle_no
    ).all()

    return render_template(

        "admin/reports.html",

        weeks=weeks,
        cycles=cycles,
        surveys = surveys,
        completed_surveys=completed_surveys,
        total_km=round(total_km,2),
        captains=captains,
        total_hours=total_hours
    )


@admin_bp.route("/reports/export")
def export_report():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "admin":
        return redirect("/")

    from io import BytesIO
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font

    # -------------------------
    # SAME FILTERS AS REPORT PAGE
    # -------------------------

    query = Survey.query.filter(
     Survey.task1_completed.is_(True),
     Survey.task2_completed.is_(True),
     Survey.survey_form_completed.is_(True)
)

    week = request.args.get("week")
    cycle = request.args.get("cycle")
    team = request.args.get("team")
    state = request.args.get("state")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    print("Selected State:", state)

    from datetime import datetime, timedelta

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        query = query.filter_by(
            cycle_no=cycle_no
        )

    project_start = datetime(2026, 6, 22)

    week_no = safe_week(week)

    if week_no is not None:

        start = project_start + timedelta(
            days=(week_no - 1) * 7
        )

        end = start + timedelta(days=7)

        query = query.filter(
            Survey.start_time >= start,
            Survey.start_time < end
        )

    from_dt = safe_date(from_date)
    to_dt = safe_date(to_date)

    if from_dt:

        query = query.filter(
            Survey.start_time >= from_dt
        )

    if to_dt:

        query = query.filter(
            Survey.start_time <
            to_dt + timedelta(days=1)
        )

    if team == "Krish":

        query = query.filter(
            Survey.state.in_([
                "WEST BENGAL",
                "ASSAM",
                "BIHAR"
            ])
        )

    elif team == "Godbole":

        query = query.filter(
            Survey.state == "ODISHA"
        )

    elif team == "Aspizo":

        query = query.filter(
            Survey.state.in_([
                "UTTAR PRADESH",
                "JHARKHAND"
            ])
        )

    # State

    if state:
        query = query.filter(
        Survey.state == state
    )

    surveys = query.order_by(
        Survey.start_time.desc()
    ).all()

    # -------------------------
    # EXCEL
    # -------------------------

    wb = Workbook()

    ws = wb.active

    ws.title = "Survey Report"

    headers = [

        "Date",
        "Cycle",
        "Captain",
        "State",
        "Section No",
        "UPC Code",
        "Stretch",
        "KM",
        "Survey Type",
        "Status"

    ]

    for col, header in enumerate(headers, start=1):

        cell = ws.cell(
            row=1,
            column=col
        )

        cell.value = header
        cell.font = Font(bold=True)

    row = 2

    for survey in surveys:

        ws.cell(
            row=row,
            column=1
        ).value = (
            survey.start_time.strftime("%d-%m-%Y")
            if survey.start_time
            else ""
        )

        ws.cell(row=row,column=2).value = survey.cycle_no
        ws.cell(row=row,column=3).value = survey.captain_name
        ws.cell(row=row,column=4).value = survey.state
        ws.cell(row=row,column=5).value = survey.section_no
        ws.cell(row=row,column=6).value = survey.upc_code
        ws.cell(row=row,column=7).value = survey.stretch_code
        ws.cell(row=row,column=8).value = survey.section_length
        ws.cell(row=row,column=9).value = survey.survey_type
        ws.cell(row=row,column=10).value = survey.status

        row += 1

    output = BytesIO()

    wb.save(output)

    output.seek(0)

    return send_file(

        output,

        as_attachment=True,

        download_name="Survey_Report.xlsx",

        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    )


@admin_bp.route("/admin/gmail-drafts")
def gmail_drafts():

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "admin":
        return redirect("/")

    return render_template(
        "admin/gmail_drafts.html"
    )


@admin_bp.route("/admin/gmail-drafts/survey-dates/<int:survey_id>")
def survey_dates_for_gmail_draft(survey_id):

    if not session.get("user_id") or session.get("role") != "admin":
        return jsonify({"start_date": None, "end_date": None}), 403

    survey = Survey.query.get_or_404(survey_id)
    print(
        f"[GEMINI SURVEY DATES] database survey_id={survey_id} "
        f"section={survey.section_no} stretch={survey.stretch_code} "
        f"has_form_url={bool(survey.end_survey_pdf)} "
        f"cached_end_date={survey.extracted_survey_end_date}",
        flush=True,
    )
    if not survey.end_survey_pdf:
        print(
            f"[GEMINI SURVEY DATES] survey_id={survey_id} has no survey form URL",
            flush=True,
        )
        return jsonify({"start_date": None, "end_date": None})

    # Prefer the date already saved in the database so the gmail-draft page
    # never burns free-tier Gemini quota for a survey we already extracted.
    # The Gemini extractor only returns an end date (no start date), so this
    # endpoint never has a start date to expose.
    if survey.extracted_survey_end_date:
        print(
            f"[GEMINI SURVEY DATES] survey_id={survey_id} "
            f"using stored end_date={survey.extracted_survey_end_date}",
            flush=True,
        )
        return jsonify({
            "start_date": None,
            "end_date": survey.extracted_survey_end_date.isoformat(),
        })

    try:
        dates = extract_survey_dates_from_drive(survey.end_survey_pdf)
        print(
            f"[GEMINI SURVEY DATES] survey_id={survey_id} extracted={dates}",
            flush=True,
        )
    except Exception as exc:
        log.exception(
            "Gemini survey-date extraction failed for survey_id=%s",
            survey_id,
        )
        print(
            f"[GEMINI SURVEY DATES] survey_id={survey_id} error={exc}",
            flush=True,
        )
        return jsonify({"start_date": None, "end_date": None}), 502

    survey.extracted_survey_end_date = datetime.strptime(
        dates["end_date"], "%Y-%m-%d"
    ).date()
    survey.survey_end_date_confidence = dates["end_confidence"]
    db.session.commit()

    return jsonify({
        "start_date": None,
        "end_date": survey.extracted_survey_end_date.isoformat(),
    })


@admin_bp.route("/admin/defect-delay-sweep")
def defect_delay_sweep():
    """Process the queued (pending) defect-delay surveys for this request.

    The delayed-surveys page polls this. Each call processes up to ``limit``
    (default 6) queued surveys; the page keeps calling while some remain, so a
    20-30 survey batch drains in a few minutes without any click blocking.
    """
    if not session.get("user_id") or session.get("role") not in ("admin", "team_leader"):
        return jsonify({"processed": 0, "remaining": 0}), 403

    from utils.auto_sync import process_pending_defect_delays

    try:
        limit = int(request.args.get("limit", 6))
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(limit, 50))

    processed, remaining = process_pending_defect_delays(
        current_app._get_current_object(),
        limit=limit,
    )
    return jsonify({"processed": processed, "remaining": remaining})

@admin_bp.route(
    "/admin/gmail-drafts/<email_type>",
    methods=["GET", "POST"]
)
def email_draft(email_type):

    if not session.get("user_id"):
        return redirect("/")

    if session.get("role") != "admin":
        return redirect("/")

    if email_type not in [
    "defect",
    "raw",
    "discrepancy",
    "cancelled"
]:
     return redirect("/admin/gmail-drafts")

    project_start = datetime(2026, 6, 22)
    today = datetime.utcnow()

    total_weeks = (
        (today.date() - project_start.date()).days // 7
    ) + 1

    weeks = list(range(1, total_weeks + 1))

    cycles = (
        db.session.query(Survey.cycle_no)
        .distinct()
        .order_by(Survey.cycle_no)
        .all()
    )

    surveys = []
    no_records = False

    selected_week = request.form.get("week") or request.args.get("week", "")
    selected_section = request.form.get("survey_id", "")
    upc_code = request.form.get("upc_code", "").strip()
    cycle_no = request.form.get("cycle_no", "")

    start_date = request.form.get("start_date", "")
    end_date = request.form.get("end_date", "")

    subject = None
    email_html = None

    # --------------------------------------------------
    # LOAD SURVEYS
    # --------------------------------------------------

    query = Survey.query.options(
        defer(Survey.end_survey_pdf),
        defer(Survey.end_survey_photo),
        defer(Survey.dashcam_photo),
        defer(Survey.settings_photo),
        defer(Survey.defect_report_file),
        defer(Survey.raw_video_excel_file),
    ).filter(
        Survey.status.in_([
            "ongoing",
            "video_uploaded_pending_form",
            "groundwork_completed",
            "cancelled",
            "video_pending",
            "rescheduled",
            "completed",
        ])
    )

    if selected_week:
        week_no = int(selected_week)

        start = project_start + timedelta(days=(week_no - 1) * 7)
        end = start + timedelta(days=7)

        query = query.filter(
            Survey.start_time >= start,
            Survey.start_time < end,
        )

    if upc_code:
        query = query.filter(
            Survey.upc_code.ilike(f"%{upc_code}%")
        )

    if cycle_no:
        query = query.filter(
            Survey.cycle_no == int(cycle_no)
        )

    surveys = (
        query.order_by(
            Survey.section_no,
            Survey.cycle_no,
        )
        .all()
    )

    # --------------------------------------------------
    # ADD MISSED SURVEYS
    # --------------------------------------------------

    missed_query = SurveyAssignment.query.filter_by(
        status="missed"
    )

    if upc_code:
        missed_query = missed_query.filter(
            SurveyAssignment.upc_code.ilike(f"%{upc_code}%")
        )

    missed_assignments = missed_query.all()
    history_query = MissedSurveyHistory.query

    if upc_code:
        history_query = history_query.filter(
            MissedSurveyHistory.upc_code.ilike(f"%{upc_code}%")
        )

    missed_history = history_query.order_by(
        MissedSurveyHistory.missed_date.desc(),
        MissedSurveyHistory.section_no
    ).all()

    # Resolve all latest surveys in one query instead of one query per missed
    # assignment/history row. This route is hit repeatedly while filtering.
    missed_sections = {
        row.section_no
        for row in (*missed_assignments, *missed_history)
        if row.section_no
    }
    latest_by_section = {}
    if missed_sections:
        latest_rows = Survey.query.filter(
            Survey.section_no.in_(missed_sections)
        ).order_by(
            Survey.section_no,
            Survey.cycle_no.desc(),
            Survey.id.desc(),
        ).all()
        latest_by_section = {}
        for row in latest_rows:
            latest_by_section.setdefault(row.section_no, row)

    for assignment in missed_assignments:
        latest = latest_by_section.get(assignment.section_no)
        assignment.missed_id = f"missed-{assignment.id}"
        assignment.cycle_no = latest.cycle_no + 1 if latest else 1
        assignment.is_missed = True
        surveys.append(assignment)

    for history in missed_history:
       
        history.missed_id = f"missed-{history.id}"
        history.is_missed = True

        surveys.append(history)

    surveys.sort(
        key=lambda x: (
            str(x.section_no),
            int(x.cycle_no),
        )
    )

    if (
        request.method == "POST"
        and request.form.get("action") != "generate"
    ):
        if not surveys:
            no_records = True

    # --------------------------------------------------
    # GENERATE EMAIL
    # --------------------------------------------------

    if (
        request.method == "POST"
        and request.form.get("action") == "generate"
        and selected_section
    ):

        is_missed = False

        if selected_section.startswith("missed-"):

            is_missed = True

            assignment_id = int(
                selected_section.replace("missed-", "")
            )

            assignment = SurveyAssignment.query.get_or_404(
                assignment_id
            )

            missed_cycle = assignment.cycle_no

            survey = Survey(
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
        section_length=assignment.section_length,
        cycle_no=missed_cycle,
        status="missed"
    )

        elif selected_section.startswith("history-"):
           
            is_missed = True

            history_id = int(
                selected_section.replace("history-", "")
            )

            history = MissedSurveyHistory.query.get_or_404(
                history_id
            )

            survey = latest_by_section.get(history.section_no)

            if survey is None:
                survey = Survey(
                     section_no=history.section_no,
                     upc_code=history.upc_code,
                     survey_day=history.survey_day,
                     captain_name=history.main_person,
                     state=history.state,
                     cycle_no=history.cycle_no,
                     status="missed"
        )

            original_cycle = history.cycle_no

        
        else:

            survey = Survey.query.get_or_404(
                safe_int(selected_section, minimum=1) or 0
            )

        # Keep manually entered values, but fill missing values from the
        # signed survey form when the draft is generated directly.
        if not start_date or not end_date:
         flash(
            "Survey Start Date and Survey End Date are mandatory.",
            "warning"
        )
         return redirect(request.url)

        if selected_section.startswith("missed-"):
         survey.cycle_no = assignment.cycle_no

        elif selected_section.startswith("history-"):
         survey.cycle_no = history.cycle_no

        else:
         original_cycle = survey.cycle_no

        # --------------------------------------------------
        # Validation
        # --------------------------------------------------

        # Your validation code here

        # --------------------------------------------------
        # Build Email
        # --------------------------------------------------

        subject = build_subject(
            survey,
            email_type,
            end_date,
        )

        subject = " ".join(str(subject).split())

        email_html = build_email_body(
            survey,
            email_type,
            start_date,
            end_date,
            selected_week,
        )

        # --------------------------------------------------
        # Attachment
        # --------------------------------------------------

        if email_type == "defect":

            attachment_url = survey.defect_report_file

            attachment_name = (
                f"{survey.upc_code}_Cycle-{survey.cycle_no}_Defect_Report.xlsx"
            )

        elif email_type == "raw":

            attachment_url = survey.raw_video_excel_file

            attachment_name = (
                f"{survey.upc_code}_Cycle-{survey.cycle_no}_Raw_Data.xlsx"
            )

        elif email_type == "cancelled":

            attachment_url = ""

            attachment_name = ""
       
        elif email_type == "discrepancy":

            attachment_url = ""
            attachment_name = ""

        attachment_bytes = None

        if attachment_url:
            attachment_bytes = download_file_from_drive(
                attachment_url
            )

        # --------------------------------------------------
# Create Gmail Draft
# --------------------------------------------------

# Existing RO CC — keep this for ALL email types
        cc_email = get_cc_email(survey.ro)

# For CANCELLED surveys only:
# Add the PIU email(s) to the existing CC.
        if email_type == "cancelled":

           piu_email = PIU_EMAIL_MAPPING.get(
           (survey.piu or "").strip(),
            ""
    )

           if piu_email:

            if cc_email:
             cc_email = f"{cc_email}, {piu_email}"
            else:
             cc_email = piu_email


        draft_id = create_gmail_draft(
            subject=subject,
            html_body=email_html,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_name,
            cc_email=cc_email,
)

        if is_missed:
            survey.cycle_no = original_cycle

        # --------------------------------------------------
        # Open Gmail Draft
        # --------------------------------------------------



        flash(
            (
                f"{survey.section_no}_Cycle{survey.cycle_no} "
                f"Gmail draft created successfully.\n"
                f"The draft has been created in "
                f"adordashcam@gmail.com. "
                f"Please open Gmail → Drafts and send the email."
            ),
            "success",
        )

        return redirect(
            url_for(
                "admin.email_draft",
                email_type=email_type,
                week=selected_week,
            )
        )

    return render_template(
        "admin/email_draft.html",
        email_type=email_type,
        weeks=weeks,
        cycles=cycles,
        surveys=surveys,
        no_records=no_records,
        selected_week=selected_week,
        selected_section=selected_section,
        upc_code=upc_code,
        cycle_no=cycle_no,
        start_date=start_date,
        end_date=end_date,
        subject=subject,
        email_html=email_html,
    )


@admin_bp.route("/admin/remark/<int:survey_id>")
def admin_view_remark(survey_id):

    if session.get("role") != "admin":
        return redirect("/")

    survey = Survey.query.get_or_404(survey_id)

    return render_template(
        "admin/view_remark.html",
        survey=survey
    )


@admin_bp.route("/admin/extract-list")
def extract_list():

    if session.get("role") != "admin":
        return redirect("/")

    status = request.args.get("status")
    stretch = request.args.get("stretch")
    state = request.args.get("state")
    captain_name = request.args.get("captain_name")
    cycle = request.args.get("cycle")
    week = request.args.get("week")
    day = request.args.get("day")
    agency = request.args.get("agency")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    query = Survey.query.filter_by(
        show_on_dashboard=True
    )

    if status:
        if status == "pdf_reupload_required":
           query = query.filter(
              Survey.pdf_reupload_required.is_(True)
           )
        else:
           query = query.filter_by(status=status)

    if stretch:
        query = query.filter(
            Survey.stretch_code.ilike(f"%{stretch}%")
        )

    if state:
        query = query.filter_by(state=state)

    if agency == "krish":

     query = query.filter(
        Survey.state.in_([
            "MEGHALAYA",
            "WEST BENGAL",
            "ASSAM",
            "BIHAR"
        ])
    )

    elif agency == "godbole":

     query = query.filter(
        Survey.state == "ODISHA"
    )

    elif agency == "aspizo":

     query = query.filter(
        Survey.state.in_([
            "UTTAR PRADESH",
            "JHARKHAND"
        ])
    )

    if captain_name:
        query = query.filter_by(
            captain_name=captain_name
        )

    cycle_no = safe_int(cycle)

    if cycle_no is not None:
        query = query.filter_by(cycle_no=cycle_no)

    if day:
        query = query.filter_by(survey_day=day)

    project_start = datetime(2026, 6, 22)

    week_no = safe_week(week)

    if week_no is not None:

        start = project_start + timedelta(
            days=(week_no-1)*7
        )

        end = start + timedelta(days=7)

        query = query.filter(
            Survey.start_time >= start,
            Survey.start_time < end
        )

    from_dt = safe_date(from_date)
    to_dt = safe_date(to_date)

    if from_dt:
        query = query.filter(Survey.start_time >= from_dt)

    if to_dt:
        query = query.filter(
            Survey.start_time < to_dt + timedelta(days=1)
        )

    surveys = query.order_by(
        Survey.section_no,
        Survey.cycle_no
    ).all()

    return render_template(
        "admin/extract_list.html",
        surveys=surveys
    )

@admin_bp.route("/test-gemini-dates/<int:survey_id>")
def test_gemini_dates(survey_id):

    survey = Survey.query.get_or_404(survey_id)

    if not survey.end_survey_pdf:
        return {
            "error": "This survey does not have a Google Drive PDF."
        }

    try:

        # -----------------------------------
        # DOWNLOAD PDF FROM GOOGLE DRIVE
        # -----------------------------------

        pdf_bytes = download_file_from_drive(
            survey.end_survey_pdf
        )

        # -----------------------------------
        # SEND PDF TO GEMINI
        # -----------------------------------

        dates = extract_survey_dates_from_pdf(
            pdf_bytes
        )

        return {
            "survey_id": survey.id,
            "start_date": dates.get("start_date"),
            "end_date": dates.get("end_date")
        }

    except Exception as e:

        return {
            "error": str(e)
        }, 500

