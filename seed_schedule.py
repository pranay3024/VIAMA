from app import app, db
from models.db_models import SurveySchedule

with app.app_context():

    schedule = SurveySchedule(
        captain_email="captain1@viama.com",
        survey_day="Monday",
        stretch_code="WB-17",
        state="West Bengal",
        main_person="Nitin",
        survey_type="Day",
        dashcam_code="D1",
        powerbank_code="P1"
    )

    db.session.add(schedule)
    db.session.commit()

print("Schedule Added")