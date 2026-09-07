from app import app, db
from werkzeug.security import generate_password_hash
from models.db_models import User

with app.app_context():

    admin = User(
        name="Viama Admin",
        email="admin@viama.com",
        password_hash=generate_password_hash("admin123"),
        role="admin"
    )

    captain = User(
        name="Captain A",
        email="captain1@viama.com",
        password_hash=generate_password_hash("captain123"),
        role="captain"
    )

    db.session.add(admin)
    db.session.add(captain)

    db.session.commit()

print("Users Created")