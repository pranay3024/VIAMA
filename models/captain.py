from app import db

class Captain(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100))

    mobile = db.Column(db.String(20))

    survey_type = db.Column(db.String(20))

    dashcam_id = db.Column(db.Integer)

    powerbank_id = db.Column(db.Integer)