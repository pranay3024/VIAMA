from flask import Flask
from extensions import db
import logging
import os
from dotenv import load_dotenv

# Ensure dotenv is loaded before anything else
# Use the correct path based on whether the app is run from root or VIAMA-main
env_path = '.env' if os.path.exists('.env') else 'VIAMA-main/.env'
load_dotenv(env_path)

from routes.regional import regional_bp
from routes.teamleader import teamleader_bp



def create_app():

    app = Flask(__name__)

    app.config.from_object("config.Config")

    # `app.secret_key` used to be reassigned here to the literal "viama_secret",
    # which silently overrode whatever config.Config computed. The key now comes
    # from SECRET_KEY via config.Config and this line is deliberately gone.
    #dwndwdisjdnsak

    db.init_app(app)

    # Establish the remote database connection during startup so the first
    # login does not pay the Supabase connection setup cost.
    try:
        with app.app_context():
            db.session.execute(db.text("SELECT 1"))
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Database pre-warm skipped: %s",
            exc,
        )

    from routes.auth import auth_bp
    from routes.admin import admin_bp
    from routes.captain import captain_bp
    from routes.roadvision import roadvision_bp
    from routes.sync import sync_bp


    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(captain_bp)
    app.register_blueprint(regional_bp)
    app.register_blueprint(teamleader_bp)
    app.register_blueprint(roadvision_bp)
    app.register_blueprint(sync_bp)

    # JSON API at /api/v1 - additive; none of the routes above are affected.
    from core.api import register_api

    register_api(app)

    # Writes the RoadVision remark as soon as a survey is marked completed,
    # instead of waiting for the nightly cron. Installed here rather than inside
    # register_api so that turning webhook capture off does not silently take
    # the remark automation with it.
    from utils.video_count_hook import install_video_count_hook

    install_video_count_hook(app)

    return app

app = create_app()

if __name__ == "__main__":
    app.run()