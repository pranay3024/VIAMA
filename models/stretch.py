from app import db

class Stretch(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    stretch_code = db.Column(db.String(50))

    state = db.Column(db.String(50))

    main_person = db.Column(db.String(100))

    captain_id = db.Column(db.Integer)