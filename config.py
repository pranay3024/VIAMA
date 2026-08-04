import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

#: The key this app shipped with. Kept only as a fallback so an environment that
#: has not set SECRET_KEY yet keeps working instead of failing to boot - it is
#: not a secret, it is in the git history of every copy of this project.
_LEGACY_SECRET_KEY = "viama_secret"


def _secret_key():
    key = (os.getenv("SECRET_KEY") or "").strip()

    if key:
        return key

    log.warning(
        "SECRET_KEY is not set - falling back to the public default. "
        "Session cookies on this deployment are forgeable. "
        "Generate one with: python -m core.cli gen-secret"
    )

    return _LEGACY_SECRET_KEY


class Config:
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True
    }
