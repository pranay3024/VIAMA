from flask import Flask
from extensions import db
from routes.regional import regional_bp
from routes.teamleader import teamleader_bp



def create_app():

    app = Flask(__name__)

    app.config.from_object("config.Config")

    # `app.secret_key` used to be reassigned here to the literal "viama_secret",
    # which silently overrode whatever config.Config computed. The key now comes
    # from SECRET_KEY via config.Config and this line is deliberately gone.

    db.init_app(app)

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