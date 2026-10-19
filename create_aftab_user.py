"""Create or update the dedicated Aftab delayed-survey login.

Run with the target DATABASE_URL configured. The password is prompted securely
and is never stored in this script or printed.
"""

from getpass import getpass

from app import app, db
from models.db_models import User
from werkzeug.security import generate_password_hash


def main():
    email = input("Aftab email address: ").strip().lower()
    if not email or "@" not in email:
        raise SystemExit("Enter a valid email address.")

    password = getpass("Aftab password: ")
    confirmation = getpass("Confirm password: ")
    if len(password) < 12:
        raise SystemExit("Use a password with at least 12 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    with app.app_context():
        user = User.query.filter_by(username="aftab").first()
        email_owner = User.query.filter_by(email=email).first()
        if email_owner is not None and email_owner is not user:
            raise SystemExit("That email address is already assigned to another user.")

        if user is None:
            user = User(
                username="aftab",
                name="Aftab",
                email=email,
                role="aftab",
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
        else:
            user.name = "Aftab"
            user.email = email
            user.role = "aftab"
            user.password_hash = generate_password_hash(password)

        db.session.commit()

    print("Aftab login is ready. Sign in with username: aftab")


if __name__ == "__main__":
    main()
