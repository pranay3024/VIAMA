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

    defect_report_file = db.Column(db.String(500))

    raw_video_excel_file = db.Column(db.String(500))
    


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