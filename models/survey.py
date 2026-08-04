from app import db
from datetime import datetime

class Survey(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    captain_id = db.Column(db.Integer)

    stretch_id = db.Column(db.Integer)

    survey_day = db.Column(db.String(20))

    status = db.Column(db.String(20))

    start_time = db.Column(db.DateTime)

    end_time = db.Column(db.DateTime)

    video_uploaded = db.Column(db.Boolean, default=False)