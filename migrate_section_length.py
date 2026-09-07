from app import app
from extensions import db
from models.db_models import Survey, SurveyAssignment

with app.app_context():

    updated = 0

    assignments = {
        a.section_no: a.section_length
        for a in SurveyAssignment.query.all()
    }

    for survey in Survey.query.all():

        if survey.section_no in assignments:

            survey.section_length = assignments[
                survey.section_no
            ]

            updated += 1

    db.session.commit()

    print(f"Updated {updated} surveys.")