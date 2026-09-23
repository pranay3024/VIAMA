from extensions import db

class User(db.Model):
    __tablename__ = "users"

    username = db.Column(
    db.String(100),
    unique=True
)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    region = db.Column(
    db.String
)
    state = db.Column(db.String(100))


class Equipment(db.Model):
    __tablename__ = "equipment"

    id = db.Column(db.Integer, primary_key=True)
    equipment_code = db.Column(db.String(20))
    equipment_type = db.Column(db.String(20))


class SurveySchedule(db.Model):
    __tablename__ = "survey_schedule"

    id = db.Column(db.Integer, primary_key=True)

    captain_email = db.Column(db.String(150))

    stretch_code = db.Column(db.String(50))
    state = db.Column(db.String(50))
    main_person = db.Column(db.String(100))
    survey_day = db.Column(db.String(20))
    survey_type = db.Column(db.String(20))
    dashcam_code = db.Column(db.String(20))
    powerbank_code = db.Column(db.String(20))


from datetime import datetime

class Survey(db.Model):
    __tablename__ = "surveys"


    end_survey_pdf = db.Column(db.Text) 

    section_length = db.Column(
    db.Float,
    default=0
)
    

    nh_number = db.Column(db.String(100))

    ro = db.Column(db.String(100))

    piu = db.Column(db.String(100))

    survey_pdf_uploaded_at = db.Column(db.DateTime, nullable=True)
 
    id = db.Column(db.Integer, primary_key=True)

    captain_email = db.Column(db.String(150))

    stretch_code = db.Column(db.Text)

    survey_day = db.Column(db.String(20))

    survey_type = db.Column(db.String(20))

    status = db.Column(db.String(20))

    start_time = db.Column(db.DateTime)

    end_survey_photo = db.Column(db.Text)

    captain_name = db.Column(db.String(100))
    
    state = db.Column(db.String(100))

    section_no = db.Column(db.String(50))

    end_time = db.Column(db.DateTime)

    pdf_reupload_required = db.Column(
    db.Boolean,
    default=False
)

    video_pending_start_time = db.Column(
    db.DateTime
)
    
    video_upload_time = db.Column(
    db.DateTime
)
    cycle_no = db.Column(
    db.Integer,
    default=1
)

    is_resurvey = db.Column(
    db.Boolean,
    default=False
)

    resurvey_requested = db.Column(
    db.Boolean,
    default=False
)
    
    task1_completed = db.Column(
    db.Boolean,
    default=False
)

    task2_completed = db.Column(
    db.Boolean,
    default=False
)
    survey_form_completed = db.Column(
    db.Boolean,
    default=False
)

    survey_form_approved = db.Column(
    db.Boolean,
    default=False,
    nullable=False
)
    
    show_on_dashboard = db.Column(
    db.Boolean,
    default=True
)
    
    show_in_teamleader_dashboard = db.Column(
    db.Boolean,
    default=True
)

    resurvey_approved = db.Column(
    db.Boolean,
    default=False
)
    dashcam_photo = db.Column(db.Text)

    settings_photo = db.Column(db.Text)

    video_uploaded = db.Column(
        db.Boolean,
        default=False
    )
    
    upc_code = db.Column(db.String(100))

    pdf_reupload_reason = db.Column(db.Text)

    pdf_reupload_count = db.Column(
    db.Integer,
    default=0
    )

    # Role that last requested the PDF re-upload (admin / form_approver), so an
    # admin can see who triggered it. NULL when nobody has requested one yet.
    pdf_reupload_requested_by = db.Column(
    db.String(30)
    )

    ir_lhs_count = db.Column(
    db.Integer,
    default=0
)

    mcw_lhs_count = db.Column(
    db.Integer,
    default=0
)

    service_lhs_count = db.Column(
    db.Integer,
    default=0
)

    slip_lhs_count = db.Column(
    db.Integer,
    default=0
)

    ir_rhs_count = db.Column(
    db.Integer,
    default=0
)

    mcw_rhs_count = db.Column(
    db.Integer,
    default=0
)

    service_rhs_count = db.Column(
    db.Integer,
    default=0
)

    slip_rhs_count = db.Column(
    db.Integer,
    default=0
)
    

    roadvision_completed = db.Column(
    db.Boolean,
    default=False
)

    roadvision_remark = db.Column(
    db.Text
)

    roadvision_completed_at = db.Column(
    db.DateTime
)

    # Result of the automated GCP bucket video-count check.
    # NULL = never checked. True = bucket holds at least the enlisted count.
    video_count_matched = db.Column(
    db.Boolean,
    nullable=True
)

    video_count_checked_at = db.Column(
    db.DateTime
)

    survey_form_completed_at = db.Column(db.DateTime)

    task1_completed_at = db.Column(db.DateTime)

    task2_completed_at = db.Column(db.DateTime)

    # Cached inputs for the admin defect-report delay check.
    extracted_survey_end_date = db.Column(db.Date, nullable=True)
    survey_end_date_confidence = db.Column(db.Float, nullable=True)
    # How many Gemini extraction attempts have been made for this survey's PDF.
    # Guards the budget: after MAX_AUTO_EXTRACT_ATTEMPTS the auto paths stop
    # paying for a PDF that keeps failing, leaving it for admin correction.
    end_date_extract_attempts = db.Column(db.Integer, nullable=False, default=0)
    defect_report_sent_at = db.Column(db.DateTime, nullable=True)
    defect_report_sent_confidence = db.Column(db.Float, nullable=True)
    defect_report_email_id = db.Column(db.String(255), nullable=True)
    defect_report_match_status = db.Column(db.String(30), nullable=True)
    defect_report_delay_days = db.Column(db.Integer, nullable=True)

    defect_report_file = db.Column(db.String(500))

    raw_video_excel_file = db.Column(db.String(500))

    # Fields extracted from the uploaded survey form by a SINGLE Gemini call.
    extracted_survey_start_date = db.Column(db.Date, nullable=True)
    survey_start_date_confidence = db.Column(db.Float, nullable=True)
    extracted_ae_ie_sc_name = db.Column(db.String(255), nullable=True)
    extracted_piu_name = db.Column(db.String(255), nullable=True)
    extracted_contractor_agency = db.Column(db.String(255), nullable=True)

    captain_status = db.Column(
    db.String(30),
    default="pending",
    nullable=False
)

    captain_status = db.Column(
    db.String(30),
    default="pending",
    nullable=False
)

    captain_status_reason = db.Column(
    db.Text,
    nullable=True
)

    captain_status_updated_at = db.Column(
    db.DateTime,
    nullable=True
)
    


class SurveyAssignment(db.Model):
    __tablename__ = "survey_assignments"

    id = db.Column(db.Integer, primary_key=True)

    captain_email = db.Column(db.String(150))

    stretch_code = db.Column(db.String(50))

    state = db.Column(db.String(50))

    main_person = db.Column(db.String(100))

    dashcam_code = db.Column(db.String(20))

    powerbank_code = db.Column(db.String(20))

    section_length = db.Column(db.Float)

    status = db.Column(db.String(20))

    captain_status = db.Column(
        db.String(30),
        default="pending",
        nullable=False
    )

    captain_status_reason = db.Column(
        db.Text,
        nullable=True
    )

    captain_status_updated_at = db.Column(
        db.DateTime,
        nullable=True
    )

    survey_enabled = db.Column(db.Boolean,default=False)
    survey_day = db.Column(db.String(20))

    section_no = db.Column(
    db.String(50)
)
    
    survey_type = db.Column(
    db.String(20),
    default="Day"
)


    missed_reason = db.Column(
    db.Text)

    alert_acknowledged = db.Column(
    db.Boolean,
    default=False
)
    
    nh_number = db.Column(db.String(100))

    ro = db.Column(db.String(100))

    piu = db.Column(db.String(100))



    missed_alert = db.Column(
        db.Boolean,
        default=False
    )

    deadline_time = db.Column(db.DateTime)

    alert_generated = db.Column(db.Boolean,default=False)
    
    last_week_reset = db.Column(
    db.Date,
    nullable=True
)
    
    upc_code = db.Column(db.String(100))

    cycle_no = db.Column(
    db.Integer,
    default=1,
    nullable=False
)





class RegionalManagerState(db.Model):

    __tablename__ = "regional_manager_states"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    manager_email = db.Column(
        db.String,
        nullable=False
    )

    state = db.Column(
        db.String,
        nullable=False
    )



class MissedSurveyHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    section_no = db.Column(db.String(100), nullable=False)
    cycle_no = db.Column(db.Integer, nullable=False)
    upc_code = db.Column(db.String(255))
    survey_day = db.Column(db.String(50))
    main_person = db.Column(db.String(255))
    state = db.Column(db.String(100))

    missed_date = db.Column(db.Date, nullable=False)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )