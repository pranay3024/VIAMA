from app import db

class User(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), nullable=False)

    mobile = db.Column(db.String(20), unique=True)

    password = db.Column(db.String(255))

    role = db.Column(db.String(20))