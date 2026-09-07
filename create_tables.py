from app import app
from extensions import db
from models.db_models import *

with app.app_context():
    db.create_all()

print("All tables created successfully")