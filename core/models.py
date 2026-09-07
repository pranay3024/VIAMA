"""
VIAMA - database tables owned by the API.

All new tables; nothing here alters the portal's schema.  Created with:

    python -m core.cli create-tables
"""

# ==========================================================================
# api_models
# Tables owned by the JSON API.
#
# Separate from models/db_models.py so the portal's schema stays untouched.  All
# six are new tables - nothing here alters an existing one.  Created by::
#
#     python -m core.cli create-tables
#
# Consistent with the rest of this schema, there are no foreign keys.
# ==========================================================================

from datetime import datetime

from extensions import db

#: Postgres turns a BigInteger PK into BIGSERIAL, but SQLite only autoincrements
#: plain INTEGER PRIMARY KEY - so tests against SQLite need the variant.
BigIntPK = db.BigInteger().with_variant(db.Integer, "sqlite")


class ApiToken(db.Model):
    """
    Registry of issued API tokens.

    The JWTs are long-lived and carry no meaningful ``exp``, so this table is the
    revocation mechanism: flip ``revoked`` and the token stops working within
    ``API_TOKEN_CACHE_TTL`` seconds.  A row must exist for a token to be
    accepted, which also means a leaked secret alone is not enough to forge one.
    """

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    kind = db.Column(db.String(20), default="service")  # "service" | "user"
    scopes = db.Column(db.Text)  # comma-separated; "*" means everything
    subject = db.Column(db.String(150))
    user_id = db.Column(db.Integer)  # set for kind="user"
    revoked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by = db.Column(db.String(150))
    last_used_at = db.Column(db.DateTime)
    note = db.Column(db.Text)

    def scope_list(self):
        if not self.scopes:
            return []
        return [s.strip() for s in self.scopes.split(",") if s.strip()]


class WebhookEndpoint(db.Model):
    """A URL on the consuming VM that should receive events."""

    __tablename__ = "webhook_endpoints"

    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.Text, nullable=False)
    secret = db.Column(db.String(120), nullable=False)  # HMAC-SHA256 signing key
    events = db.Column(db.Text, default="*")  # csv of patterns, "*" = firehose
    active = db.Column(db.Boolean, default=True, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_success_at = db.Column(db.DateTime)
    last_error_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text)
    failure_count = db.Column(db.Integer, default=0, nullable=False)

    def event_patterns(self):
        if not self.events:
            return ["*"]
        return [e.strip() for e in self.events.split(",") if e.strip()]

    def wants(self, event):
        """Match ``survey.completed`` against patterns like ``survey.*`` or ``*``."""
        for pattern in self.event_patterns():
            if pattern == "*" or pattern == event:
                return True
            if pattern.endswith(".*") and event.startswith(pattern[:-1]):
                return True
        return False


class WebhookOutbox(db.Model):
    """
    Durable queue of events awaiting delivery.

    Written inside the same transaction as the business change, so an event can
    never be lost because a delivery attempt failed or the serverless function
    was frozen mid-flight.
    """

    __tablename__ = "webhook_outbox"

    id = db.Column(BigIntPK, primary_key=True)
    event = db.Column(db.String(60), nullable=False, index=True)
    entity = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer)
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    next_retry_at = db.Column(db.DateTime, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    delivered_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text)


class WebhookDelivery(db.Model):
    """One delivery attempt against one endpoint - the audit trail."""

    __tablename__ = "webhook_deliveries"

    id = db.Column(BigIntPK, primary_key=True)
    outbox_id = db.Column(BigIntPK, index=True)
    endpoint_id = db.Column(db.Integer, index=True)
    event = db.Column(db.String(60))
    attempt = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20))  # delivered | failed
    response_status = db.Column(db.Integer)
    response_body = db.Column(db.Text)
    latency_ms = db.Column(db.Integer)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ChangeLog(db.Model):
    """
    Append-only feed backing ``GET /api/v1/sync/changes``.

    The cursor is ``id``, not a timestamp - see core/res_sync.py for why, and for
    the visibility-lag guard that stops the poller silently skipping rows.
    """

    __tablename__ = "change_log"

    id = db.Column(BigIntPK, primary_key=True)
    entity = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer)
    event = db.Column(db.String(60), nullable=False)
    op = db.Column(db.String(10))  # insert | update | delete
    data = db.Column(db.JSON)
    previous = db.Column(db.JSON)
    actor = db.Column(db.String(150))
    source = db.Column(db.String(20))  # api | portal | system
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class LoginEvent(db.Model):
    """
    Authentication audit trail.

    One row per login attempt, logout, and session expiry - successes AND
    failures, because failures are what tell you about credential stuffing.

    Captured by an app-level hook (core/audit.py), so routes/auth.py needs no
    edits.  Note the portal returns a bare 200 with the string "Invalid
    Credentials" on a bad password, so failures are detected by inspecting the
    response rather than a status code.

    ``ip`` is personal data.  ``AUDIT_RETENTION_DAYS`` (default 365) controls how
    long these are kept; the purge runs from POST /api/v1/jobs/tick.
    """

    __tablename__ = "api_login_events"

    id = db.Column(BigIntPK, primary_key=True)

    # login_success | login_failed | logout | api_token_used
    event_type = db.Column(db.String(24), nullable=False, index=True)

    user_id = db.Column(db.Integer, index=True)
    username = db.Column(db.String(100))
    email = db.Column(db.String(150), index=True)
    name = db.Column(db.String(100))
    role = db.Column(db.String(20), index=True)

    #: The login identifier as typed - useful on failures, where no user exists.
    login_identifier = db.Column(db.String(150))
    failure_reason = db.Column(db.String(120))

    ip = db.Column(db.String(64), index=True)
    forwarded_for = db.Column(db.String(255))  # full X-Forwarded-For chain
    user_agent = db.Column(db.Text)
    device_type = db.Column(db.String(20))  # mobile | tablet | desktop | bot
    browser = db.Column(db.String(60))
    platform = db.Column(db.String(60))

    # Vercel injects these at the edge; null when self-hosted.
    country = db.Column(db.String(8))
    region = db.Column(db.String(60))
    city = db.Column(db.String(80))
    timezone = db.Column(db.String(60))

    method = db.Column(db.String(10))
    path = db.Column(db.String(255))
    referer = db.Column(db.Text)
    host = db.Column(db.String(120))
    protocol = db.Column(db.String(10))
    request_id = db.Column(db.String(64))

    #: Stable per browser session, so a login can be tied to its later requests.
    session_id = db.Column(db.String(64), index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class RequestLog(db.Model):
    """
    Full access log - every request, portal and API alike.

    Volume control: ``AUDIT_LOG_REQUESTS`` = ``all`` (default) | ``api`` |
    ``mutations`` | ``off``.  Static assets are never logged.  Rows are purged
    after ``AUDIT_RETENTION_DAYS``.
    """

    __tablename__ = "api_request_logs"

    id = db.Column(BigIntPK, primary_key=True)

    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(255), nullable=False, index=True)
    query_string = db.Column(db.Text)
    endpoint = db.Column(db.String(120))
    status_code = db.Column(db.Integer, index=True)
    duration_ms = db.Column(db.Integer)

    #: portal (session cookie) | api (bearer token) | anonymous
    channel = db.Column(db.String(12), index=True)
    user_id = db.Column(db.Integer, index=True)
    email = db.Column(db.String(150))
    role = db.Column(db.String(20))
    token_sub = db.Column(db.String(150))
    session_id = db.Column(db.String(64), index=True)

    ip = db.Column(db.String(64), index=True)
    forwarded_for = db.Column(db.String(255))
    user_agent = db.Column(db.Text)
    device_type = db.Column(db.String(20))
    country = db.Column(db.String(8))
    city = db.Column(db.String(80))

    referer = db.Column(db.Text)
    content_length = db.Column(db.Integer)
    response_bytes = db.Column(db.Integer)
    request_id = db.Column(db.String(64), index=True)
    error_code = db.Column(db.String(60))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class DeletedRecord(db.Model):
    """
    Soft-delete tombstones.

    Deliberately a separate table rather than a ``deleted_at`` column on
    ``surveys`` / ``users`` / ``survey_assignments``.  Adding a column to those
    models would make SQLAlchemy select it on every query, so the portal would
    500 everywhere until the DDL had been applied - a two-phase deploy on a live
    system.  A side table keeps the whole feature additive: nothing about the
    portal's schema changes, and no migration has to land before the code.

    Hard delete is avoided because ``cycle_no`` is derived by scanning survey
    history (routes/captain.py:228-235); removing the newest survey rewinds the
    cycle sequence and the next one reuses a number already printed in exported
    reports and PDF filenames.
    """

    __tablename__ = "api_deleted_records"

    id = db.Column(db.Integer, primary_key=True)
    entity = db.Column(db.String(40), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=False, index=True)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    deleted_by = db.Column(db.String(150))
    reason = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint("entity", "entity_id", name="uq_api_deleted_records"),
    )


class IdempotencyKey(db.Model):
    """
    Replay cache for ``POST /surveys`` and ``POST /surveys/start``.

    DB-backed rather than in-process: on serverless there is no shared memory, so
    two concurrent invocations would otherwise both create the survey.
    """

    __tablename__ = "api_idempotency"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), nullable=False)
    endpoint = db.Column(db.String(120), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    status_code = db.Column(db.Integer)
    response = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (db.UniqueConstraint("key", "endpoint", name="uq_api_idempotency"),)


#: Everything ``create-tables`` should build.
API_TABLES = (
    ApiToken,
    WebhookEndpoint,
    WebhookOutbox,
    WebhookDelivery,
    ChangeLog,
    DeletedRecord,
    IdempotencyKey,
    LoginEvent,
    RequestLog,
)

