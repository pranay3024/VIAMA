from werkzeug.security import generate_password_hash
from models.db_models import User
from extensions import db

user = User(
    name="Assam Regional Manager",
    email="regional.up@viama.com",
    password_hash=generate_password_hash("123456"),
    role="regional_manager",
    region="Uttar Pradesh"
)

db.session.add(user)
db.session.commit()