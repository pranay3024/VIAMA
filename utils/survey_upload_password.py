"""
Shared survey-form upload password for the Krish team.

Captains in the Krish team's states (Assam, West Bengal, Meghalaya, Bihar)
must type a password before the survey-form PDF is accepted. The idea is that
only captains who have been told the password can upload a form for those
states.

The password is one shared value per team, held in
``survey_upload_passwords``. It is stored in clear text because the regional
manager dashboard has to display the current value for the manager to hand
out; a hash could not be shown back. Only the regional manager whose login id
is ``vipin`` may see or change it (see ``is_krish_manager``).

Every helper fails open. If the table has not been migrated yet, or the query
errors, ``check_upload_password`` returns True so a missing table cannot lock
captains out of finishing a survey.
"""

import hmac
import logging
from datetime import datetime

log = logging.getLogger(__name__)

#: Team key, matching core/config.py:TEAMS. Stored in the DB as-is.
KRISH_TEAM = "krish"

#: The states whose captains need the password. Uppercase, matching the
#: value stored on Survey.state / SurveyAssignment.state.
KRISH_STATES = (
    "ASSAM",
    "WEST BENGAL",
    "MEGHALAYA",
    "BIHAR",
)

#: Login ids allowed to view / change the password. The username column is
#: matched first, then the email or its local part, so either "vipin" or
#: "vipin@..." resolves to the same manager.
KRISH_MANAGER_LOGIN_IDS = ("vipin",)

#: Seeded by migrate_survey_upload_password.py so the gate is usable before
#: the manager changes it for the first time.
DEFAULT_KRISH_PASSWORD = "VIAMA@Krish"


def normalise_state(state):
    """Upper-case, trimmed state name - or "" when there is none."""

    return (state or "").strip().upper()


def state_requires_upload_password(state):
    """True when this state's captains must supply the upload password."""

    return normalise_state(state) in KRISH_STATES


def is_krish_manager(user):
    """True when ``user`` is the regional manager allowed to manage the key.

    Matches the username first, then the email and its local part against
    ``KRISH_MANAGER_LOGIN_IDS``.
    """

    if user is None:
        return False

    identifiers = set()

    username = getattr(user, "username", None)
    if username:
        identifiers.add(str(username).strip().lower())

    email = getattr(user, "email", None)
    if email:
        email = str(email).strip().lower()
        identifiers.add(email)
        identifiers.add(email.split("@")[0])

    return bool(identifiers & set(KRISH_MANAGER_LOGIN_IDS))


def get_upload_password(team=KRISH_TEAM):
    """The current shared password for ``team``, or None if never set."""

    try:
        from models.db_models import SurveyUploadPassword

        row = SurveyUploadPassword.query.filter_by(team=team).first()

        if row and row.password:
            return row.password

    except Exception:
        log.debug("could not read upload password for team %r",
                  team, exc_info=True)

    return None


def set_upload_password(password, updated_by_email=None, team=KRISH_TEAM):
    """Create or update the shared password for ``team``."""

    from extensions import db
    from models.db_models import SurveyUploadPassword

    row = SurveyUploadPassword.query.filter_by(team=team).first()

    if row is None:
        row = SurveyUploadPassword(team=team)
        db.session.add(row)

    row.password = password
    row.updated_by_email = updated_by_email
    row.updated_at = datetime.utcnow()

    db.session.commit()

    return row


def check_upload_password(state, submitted, team=KRISH_TEAM):
    """True when ``submitted`` unlocks the form upload for ``state``.

    States outside the Krish team always pass. Fails open when no password
    has been configured, so an un-migrated table cannot block uploads.
    """

    if not state_requires_upload_password(state):
        return True

    expected = get_upload_password(team)

    if not expected:
        return True

    if submitted is None:
        return False

    return hmac.compare_digest(str(submitted), str(expected))
