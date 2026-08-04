from flask import Blueprint
from flask import render_template
from flask import request
from flask import redirect
from flask import session
from flask import session
from flask import redirect

from werkzeug.security import check_password_hash

from models.db_models import User

auth_bp = Blueprint(
    "auth",
    __name__
)

from sqlalchemy import or_

@auth_bp.route("/", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        login = request.form["login"]
        password = request.form["password"]

        print("\n====================")
        print("Login Entered:", login)

        user = User.query.filter(
            or_(
                User.email == login,
                User.username == login
            )
        ).first()

        print("User Found:", user)

        if user:

            print("User ID:", user.id)
            print("Role:", user.role)

            password_match = check_password_hash(
                user.password_hash,
                password
            )

            if password_match:

                session["user_id"] = user.id
                session["role"] = user.role

                if user.role == "admin":
                    return redirect("/admin")

                if user.role == "captain":
                    return redirect("/captain-home")
                
                if user.role == "regional_manager":
                    return redirect("/regional")
                
                elif user.role == "team_leader":
                    return redirect("/teamleader")
                
                elif user.role == "backup_captain":
                    return redirect("/backup-home")
                
                elif user.role == "roadvision":
                    return redirect("/roadvision")

        return "Invalid Credentials"

    return render_template("auth/login.html")
@auth_bp.route("/logout")
def logout():

    session.clear()

    return redirect("/")