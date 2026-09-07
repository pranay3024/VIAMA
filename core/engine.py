"""
VIAMA - API machinery.

Auth, error handling, response envelopes, query parsing, serialization, generic
CRUD, the survey workflow, the missed-survey engine, media uploads, change
capture, audit logging and webhook delivery.

Nothing in here defines a route; see core/endpoints.py for those.
"""

# ==========================================================================
# errors
# Error envelope and HTTP status conventions for /api/v1.
#
# Two rules that matter:
#
# 1. The API never redirects.  The portal's guards do ``return redirect("/")`` on
#    an auth failure, which is useless to a machine client - here it is a 401/403
#    JSON body.
#
# 2. The handlers are registered app-wide (routing-level 404/405 never reach a
#    blueprint) but every one of them checks ``request.path`` first and re-raises
#    for non-API paths, so the portal's HTML error pages are untouched.
# ==========================================================================

import logging
import traceback
import uuid

from flask import g, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger("viama.api")

API_PREFIX = "/api/"


class ApiError(Exception):
    """Base for every error the API raises deliberately."""

    status = 500
    code = "internal_error"
    message = "Something went wrong."

    def __init__(self, message=None, details=None, code=None, status=None, headers=None):
        super().__init__(message or self.message)
        if message:
            self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or []
        self.headers = headers or {}

    def to_dict(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "status": self.status,
                "details": self.details,
                "request_id": getattr(g, "request_id", None),
            }
        }


class BadRequest(ApiError):
    status, code, message = 400, "bad_request", "The request could not be parsed."


class Unauthorized(ApiError):
    status, code, message = 401, "unauthorized", "Authentication is required."

    def __init__(self, message=None, code=None, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("WWW-Authenticate", 'Bearer realm="viama-api"')
        super().__init__(message=message, code=code, headers=headers, **kwargs)


class Forbidden(ApiError):
    status, code, message = 403, "forbidden", "You do not have access to this resource."


class NotFound(ApiError):
    status, code, message = 404, "not_found", "Resource not found."


class MethodNotAllowed(ApiError):
    status, code, message = 405, "method_not_allowed", "Method not allowed."


class Conflict(ApiError):
    status, code, message = 409, "conflict", "The request conflicts with current state."


class PayloadTooLarge(ApiError):
    status, code, message = 413, "payload_too_large", "Upload exceeds the size limit."


class UnsupportedMediaType(ApiError):
    status = 415
    code = "unsupported_media_type"
    message = "Unsupported Content-Type for this endpoint."


class ValidationError(ApiError):
    status, code, message = 422, "validation_error", "Request failed validation."


class RateLimited(ApiError):
    status, code, message = 429, "rate_limited", "Too many requests."


class ServiceUnavailable(ApiError):
    status = 503
    code = "upstream_unavailable"
    message = "A dependency is unavailable."


class InvalidTransition(Conflict):
    code = "invalid_state_transition"

    def __init__(self, current, target, allowed=None):
        super().__init__(
            message=f"Cannot move a survey from '{current}' to '{target}'.",
            details=[
                {
                    "field": "status",
                    "issue": "illegal transition",
                    "value": target,
                    "allowed": sorted(allowed or []),
                }
            ],
        )


def is_api_request():
    """
    True when the current request is aimed at the API rather than the portal.

    Reads the live mount point rather than assuming ``/api/``: with
    ``API_BASE_PATH=/_int/7f3a91`` the API no longer lives under ``/api/`` at all,
    and hardcoding the prefix here meant every API error fell through to the
    portal's HTML error pages instead of the JSON envelope.
    """
    try:
        from flask import current_app

        mount = current_app.config.get("VIAMA_API_BASE_PATH")
    except Exception:
        mount = None
    if not mount:
        return request.path.startswith(API_PREFIX)
    return request.path == mount or request.path.startswith(mount + "/")


def _respond(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status
    for key, value in error.headers.items():
        response.headers[key] = value
    return response


def register_error_handlers(app):
    """Attach the API's JSON error handling without affecting the portal."""

    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    @app.errorhandler(ApiError)
    def _handle_api_error(error):
        if error.status >= 500:
            log.error("api error %s: %s", error.code, error.message, exc_info=True)
        return _respond(error)

    @app.errorhandler(AuthConfigError)
    def _handle_auth_config_error(error):
        """
        A missing or unsafe ``API_JWT_SECRET`` is an operator mistake, not a
        client one.

        Without this it fell through to the catch-all below and every
        authenticated call answered a bare 500 "Something went wrong." - which
        tells whoever is wiring up the other VM nothing at all.  The remedy goes
        to the log; the client gets 503 and the reason, never the secret.
        """
        if not is_api_request():
            raise error
        log.error("api misconfigured: %s", error)
        return _respond(
            ApiError(
                message=(
                    "The API is not configured on this deployment. "
                    "Generate a signing secret with 'python -m core.cli gen-secret' "
                    "and set API_JWT_SECRET."
                ),
                code="api_misconfigured",
                status=503,
            )
        )

    @app.errorhandler(HTTPException)
    def _handle_http_exception(error):
        if not is_api_request():
            return error  # portal keeps Flask's HTML error pages
        mapped = ApiError(
            message=error.description or error.name,
            code=error.name.lower().replace(" ", "_"),
            status=error.code or 500,
        )
        if error.code == 404:
            mapped.code = "not_found"
        elif error.code == 405:
            mapped.code = "method_not_allowed"
            allow = getattr(error, "valid_methods", None)
            if allow:
                mapped.headers["Allow"] = ", ".join(allow)
        elif error.code == 413:
            mapped.code = "payload_too_large"
        return _respond(mapped)

    @app.errorhandler(Exception)
    def _handle_unexpected(error):
        if not is_api_request():
            raise error  # portal behaviour unchanged
        request_id = getattr(g, "request_id", None)
        # Full detail to the log, nothing sensitive to the client: tracebacks
        # here would leak the connection string and the Supabase key.
        log.error(
            "unhandled api exception request_id=%s path=%s\n%s",
            request_id,
            request.path,
            traceback.format_exc(),
        )
        return _respond(ApiError())


def translate_db_error(error):
    """
    Map a SQLAlchemy error onto the right ApiError.

    Unique violations become 409 rather than 500 - the only unique constraints on
    the portal schema are ``users.email`` and ``users.username``.
    """
    text = str(getattr(error, "orig", error))
    lowered = text.lower()
    if "unique" in lowered or "duplicate key" in lowered:
        field = None
        for candidate in ("email", "username", "jti", "key"):
            if candidate in lowered:
                field = candidate
                break
        return Conflict(
            message="A record with that value already exists.",
            code="duplicate_value",
            details=[{"field": field, "issue": "must be unique"}] if field else [],
        )
    if "invalid input syntax" in lowered or "datatype mismatch" in lowered:
        return ValidationError(message="A field had the wrong type for its column.")
    if "does not exist" in lowered and "column" in lowered:
        return ServiceUnavailable(
            message=(
                "The database is missing a column this build expects. "
                "Run: python -m core.cli migrate"
            ),
            code="schema_out_of_date",
        )
    return ApiError(message="Database error.", code="database_error", status=500)

# ==========================================================================
# envelope
# Response envelope.
#
# Every successful response has the same shape, so the consuming site can write
# one parser:
#
#     { "data": ..., "meta": {...}, "links": {...} }
#
# ``links`` and ``meta.pagination`` appear only on list responses.
# ==========================================================================

from flask import g, jsonify, request

from core.config import UTC_WALL, as_utc, ist_now_aware, iso, utc_now


def _base_meta():
    return {
        "request_id": getattr(g, "request_id", None),
        "generated_at_utc": iso(as_utc(utc_now(), UTC_WALL)),
        "generated_at_ist": iso(ist_now_aware()),
    }


def _finalize(payload, status, headers=None):
    response = jsonify(payload)
    response.status_code = status
    response.headers["X-Request-Id"] = getattr(g, "request_id", "") or ""
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def ok(data, meta=None, headers=None, status=200):
    payload = {"data": data, "meta": _base_meta()}
    if meta:
        payload["meta"].update(meta)
    return _finalize(payload, status, headers)


def created(data, location=None, meta=None):
    headers = {"Location": location} if location else {}
    return ok(data, meta=meta, headers=headers, status=201)


def no_content():
    response = _finalize({}, 204)
    response.set_data(b"")
    return response


def _replace_query(**overrides):
    """Rebuild the current URL with some query params replaced."""
    args = request.args.to_dict(flat=True)
    for key, value in overrides.items():
        if value is None:
            args.pop(key, None)
        else:
            args[key] = value
    from urllib.parse import urlencode

    query = urlencode(args)
    return f"{request.base_url}?{query}" if query else request.base_url


def paginated(items, page_info, meta=None, headers=None):
    """
    List response with pagination metadata and navigation links.

    ``page_info`` comes from ``core.params.ListParams.paginate()``.
    """
    total = page_info.get("total")
    page = page_info.get("page", 1)
    per_page = page_info.get("per_page", 50)
    total_pages = page_info.get("total_pages")

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_next": page_info.get("has_next", False),
        "has_prev": page > 1,
    }
    if page_info.get("next_cursor") is not None:
        pagination["next_cursor"] = page_info["next_cursor"]

    payload_meta = _base_meta()
    payload_meta["pagination"] = pagination
    if meta:
        payload_meta.update(meta)

    links = {"self": _replace_query()}
    if page_info.get("cursor_mode"):
        if page_info.get("next_cursor") is not None:
            links["next"] = _replace_query(cursor=page_info["next_cursor"], page=None)
    else:
        if pagination["has_next"]:
            links["next"] = _replace_query(page=page + 1)
        if pagination["has_prev"]:
            links["prev"] = _replace_query(page=page - 1)
        links["first"] = _replace_query(page=1)
        if total_pages:
            links["last"] = _replace_query(page=total_pages)

    all_headers = dict(headers or {})
    if total is not None:
        all_headers["X-Total-Count"] = str(total)

    return _finalize({"data": items, "meta": payload_meta, "links": links}, 200, all_headers)

# ==========================================================================
# auth
# Bearer-token authentication for /api/v1.
#
# Design constraints:
#
# * One long-lived token.  The consuming VM holds a single JWT and uses it
#   forever - no refresh dance.  A far-future ``exp`` is still set as a backstop so
#   a lost ``api_tokens`` table cannot leave an immortal credential behind.
#
# * Revocation must work.  Because the token effectively never expires, the
#   ``api_tokens`` row IS the kill switch: set ``revoked = true`` and the token
#   stops working.  A per-instance cache means revocation takes effect within
#   ``API_TOKEN_CACHE_TTL`` seconds (default 30, set 0 for immediate).  This has to
#   be DB-backed - serverless instances share no memory, so an in-process denylist
#   would only ever cover the one instance that set it.
#
# * The portal is untouched.  Nothing here reads or writes ``flask.session``, so
#   API responses carry no cookies and the two auth systems are independent.
# ==========================================================================

import functools
import hmac
import os
import time
import uuid

import jwt
from flask import g, request

# (merged) now defined in this module: Forbidden, Unauthorized

ALGORITHM = "HS256"
DEFAULT_TOKEN_LIFETIME_SECONDS = 10 * 365 * 24 * 3600  # 10 years

# jti -> (expires_at_monotonic, record | None)
_TOKEN_CACHE = {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class AuthConfigError(RuntimeError):
    """Raised when the API secret is missing or unsafe."""


def jwt_secret(required=True):
    """
    The HS256 signing key.

    Refuses the portal's hardcoded Flask key: reusing it would mean anyone who
    read config.py could mint API tokens.
    """
    secret = os.getenv("API_JWT_SECRET", "").strip()
    if not secret:
        if not required:
            return None
        raise AuthConfigError(
            "API_JWT_SECRET is not set. Generate one with:\n"
            "    python -m core.cli gen-secret"
        )
    if secret == "viama_secret":
        raise AuthConfigError(
            "API_JWT_SECRET must not reuse the Flask session key 'viama_secret'."
        )
    if len(secret) < 32:
        raise AuthConfigError("API_JWT_SECRET must be at least 32 characters.")
    return secret


def token_version():
    try:
        return int(os.getenv("API_TOKEN_VERSION", "1"))
    except ValueError:
        return 1


def cache_ttl():
    try:
        return int(os.getenv("API_TOKEN_CACHE_TTL", "30"))
    except ValueError:
        return 30


def api_enabled():
    return os.getenv("API_ENABLED", "1").strip().lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

ALL_SCOPES = (
    "users:read",
    "users:write",
    "surveys:read",
    "surveys:write",
    "assignments:read",
    "assignments:write",
    "schedules:read",
    "schedules:write",
    "equipment:read",
    "equipment:write",
    "manager_states:read",
    "manager_states:write",
    "dashboards:read",
    "reports:read",
    "exports:read",
    "alerts:read",
    "alerts:write",
    "audit:read",
    "audit:write",
    "dump:read",
    "actions:write",
    "media:write",
    "webhooks:admin",
    "jobs:run",
    "admin:destroy",
)

#: Convenient bundle for a mirror-only consumer.  Access logs are excluded on
#: purpose - they are more sensitive than survey data, so "audit:read" must be
#: granted deliberately rather than arriving with a blanket read token.
READ_ONLY_SCOPES = tuple(
    s for s in ALL_SCOPES if s.endswith(":read") and not s.startswith("audit:")
)


def scope_satisfied(granted, required):
    """``surveys:*`` covers ``surveys:read``; ``*`` covers everything."""
    for scope in granted:
        if scope == "*" or scope == required:
            return True
        if scope.endswith(":*") and required.startswith(scope[:-1]):
            return True
    return False


# ---------------------------------------------------------------------------
# The authenticated caller
# ---------------------------------------------------------------------------


class ApiClient:
    def __init__(self, claims, record=None):
        self.claims = claims
        self.record = record
        self.subject = claims.get("sub")
        self.jti = claims.get("jti")
        self.kind = claims.get("typ", "api")
        self.scopes = claims.get("scopes") or []
        self.user_id = claims.get("uid")

    def has(self, scope):
        return scope_satisfied(self.scopes, scope)

    def require(self, scope):
        if not self.has(scope):
            raise Forbidden(
                message=f"This token is missing the '{scope}' scope.",
                code="insufficient_scope",
                details=[{"field": "scopes", "issue": "missing", "value": scope}],
            )

    def to_dict(self):
        return {
            "sub": self.subject,
            "kind": self.kind,
            "scopes": list(self.scopes),
            "jti": self.jti,
            "version": self.claims.get("ver"),
            "issued_at": self.claims.get("iat"),
            "expires_at": self.claims.get("exp"),
            "user_id": self.user_id,
            "name": getattr(self.record, "name", None),
        }


def current_client():
    """The authenticated caller for this request, or None."""
    return getattr(g, "api_client", None)


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def mint_token(
    subject,
    scopes=("*",),
    name=None,
    kind="service",
    user_id=None,
    note=None,
    created_by=None,
    lifetime_seconds=DEFAULT_TOKEN_LIFETIME_SECONDS,
    persist=True,
):
    """
    Issue a token and register it so it can later be revoked.

    Returns ``(token_string, ApiToken row | None)``.  The string is shown once -
    only its ``jti`` is stored.
    """
    from extensions import db

    from core.models import ApiToken

    secret = jwt_secret()
    scopes = list(scopes)
    issued_at = int(time.time())
    jti = uuid.uuid4().hex

    claims = {
        "sub": subject,
        "typ": kind,
        "ver": token_version(),
        "scopes": scopes,
        "iat": issued_at,
        "exp": issued_at + lifetime_seconds,
        "jti": jti,
    }
    if user_id is not None:
        claims["uid"] = user_id

    token = jwt.encode(claims, secret, algorithm=ALGORITHM)
    if isinstance(token, bytes):  # PyJWT < 2 compatibility
        token = token.decode("utf-8")

    record = None
    if persist:
        record = ApiToken(
            jti=jti,
            name=name or subject,
            kind=kind,
            scopes=",".join(scopes),
            subject=subject,
            user_id=user_id,
            revoked=False,
            created_by=created_by,
            note=note,
        )
        db.session.add(record)
        db.session.commit()

    return token, record


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _extract_token():
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    api_key = request.headers.get("X-Api-Key")
    if api_key:
        return api_key.strip()
    # Deliberately NOT read from the query string: it would be captured in
    # access logs and browser history.
    return None


def _lookup_token_record(jti):
    """Fetch the api_tokens row, cached per warm instance for cache_ttl seconds."""
    from core.models import ApiToken

    ttl = cache_ttl()
    now = time.monotonic()

    if ttl > 0:
        cached = _TOKEN_CACHE.get(jti)
        if cached and cached[0] > now:
            return cached[1]

    record = ApiToken.query.filter_by(jti=jti).first()

    if ttl > 0:
        _TOKEN_CACHE[jti] = (now + ttl, record)
    return record


def decode_token(raw):
    """Verify a token string and return an :class:`ApiClient`."""
    secret = jwt_secret()
    try:
        claims = jwt.decode(raw, secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise Unauthorized(message="Token has expired.", code="token_expired")
    except jwt.InvalidTokenError:
        raise Unauthorized(message="Token is not valid.", code="invalid_token")

    if int(claims.get("ver", 0)) < token_version():
        raise Unauthorized(
            message="Token was issued before the current token version.",
            code="token_version_stale",
        )

    jti = claims.get("jti")
    if not jti:
        raise Unauthorized(message="Token is missing a jti claim.", code="invalid_token")

    record = _lookup_token_record(jti)
    if record is None:
        raise Unauthorized(
            message="Token is not registered. It may have been deleted.",
            code="unknown_token",
        )
    if record.revoked:
        raise Unauthorized(message="Token has been revoked.", code="token_revoked")

    # The registry is authoritative for scopes, so narrowing a token's access
    # does not require reissuing it.
    granted = record.scope_list() or claims.get("scopes") or []
    claims = dict(claims, scopes=granted)

    return ApiClient(claims, record)


def _touch_last_used(client):
    """Best-effort ``last_used_at`` update; never fails a request."""
    if not client or not client.record:
        return
    try:
        from datetime import datetime

        from extensions import db

        stamp = datetime.utcnow()
        last = client.record.last_used_at
        # One write per minute per token is plenty for abuse visibility.
        if last and (stamp - last).total_seconds() < 60:
            return
        client.record.last_used_at = stamp
        db.session.commit()
        _TOKEN_CACHE.pop(client.jti, None)
    except Exception:
        from extensions import db

        db.session.rollback()


def authenticate():
    """Authenticate the current request, storing the client on ``flask.g``."""
    if not api_enabled():
        from core.engine import ServiceUnavailable

        raise ServiceUnavailable(
            message="The API is disabled (API_ENABLED=0).", code="api_disabled"
        )

    raw = _extract_token()
    if not raw:
        raise Unauthorized(
            message="Provide a token via 'Authorization: Bearer <token>'.",
            code="missing_token",
        )
    client = decode_token(raw)
    g.api_client = client
    _touch_last_used(client)
    return client


def require_auth(*scopes):
    """
    Decorator: authenticate, then require every listed scope.

    ``@require_auth("surveys:read")``
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            client = authenticate()
            for scope in scopes:
                client.require(scope)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_any_scope(*scopes):
    """Decorator: authenticate, then require at least one of the listed scopes."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            client = authenticate()
            if scopes and not any(client.has(scope) for scope in scopes):
                raise Forbidden(
                    message="This token has none of the required scopes.",
                    code="insufficient_scope",
                    details=[{"field": "scopes", "issue": "one required", "value": list(scopes)}],
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_cron_secret():
    """
    Auth for scheduled endpoints, which carry no bearer token.

    Accepts ``X-Cron-Secret: <secret>`` or the ``Authorization: Bearer <secret>``
    form Vercel Cron sends when ``CRON_SECRET`` is configured.  A token with the
    ``jobs:run`` scope also works.
    """
    expected = os.getenv("CRON_SECRET", "").strip()
    if expected:
        supplied = request.headers.get("X-Cron-Secret", "").strip()
        if not supplied:
            header = request.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        if supplied and hmac.compare_digest(supplied, expected):
            g.api_client = ApiClient({"sub": "cron", "typ": "cron", "scopes": ["jobs:run"]})
            return g.api_client

    client = authenticate()
    client.require("jobs:run")
    return client


# ---------------------------------------------------------------------------
# Actor resolution for identity-scoped endpoints
# ---------------------------------------------------------------------------


def resolve_actor(explicit_email=None, required=True, roles=None):
    """
    Resolve which portal user an identity-scoped endpoint acts as.

    The captain / backup / regional dashboards are meaningless without knowing
    *whose* they are.  A service token names the user via ``?captain_email=`` (or
    ``?manager_email=`` / ``?as_user=``); a user-bound token is pinned to its own
    identity and may not impersonate anyone else.
    """
    from models.db_models import User

    from core.engine import NotFound

    client = current_client()
    email = (
        explicit_email
        or request.args.get("as_user")
        or request.args.get("captain_email")
        or request.args.get("manager_email")
        or request.args.get("actor_email")
    )

    if client and client.kind == "user":
        user = User.query.get(client.user_id) if client.user_id else None
        if user and email and email.lower() != (user.email or "").lower():
            raise Forbidden(
                message="This token is bound to a single user and cannot act as another.",
                code="impersonation_denied",
            )
        if user:
            return user

    if not email:
        if not required:
            return None
        raise Unauthorized(
            message=(
                "This endpoint is user-scoped. Identify the user with "
                "?captain_email= (or ?manager_email= / ?as_user=)."
            ),
            code="actor_required",
        )

    user = User.query.filter(User.email.ilike(email)).first()
    if not user:
        raise NotFound(message=f"No user with email '{email}'.")

    if roles and user.role not in roles:
        raise Forbidden(
            message=f"User '{email}' has role '{user.role}', expected one of {list(roles)}.",
            code="wrong_role",
        )
    return user

# ==========================================================================
# params
# Filtering, sorting and pagination - implemented once, used by every list endpoint.
#
# Conventions
# -----------
# filters     ``?status=completed``, ``?state__in=ODISHA,BIHAR``, ``?cycle_no__gte=2``
#             operators: ``__in __ne __gt __gte __lt __lte __like __startswith __isnull``
# search      ``?q=text`` - ILIKE across the resource's searchable columns
# sort        ``?sort=-start_time,section_no`` (leading ``-`` = DESC), or a named
#             preset like ``?sort=admin_rank``
# paging      ``?page=1&per_page=50``  or keyset ``?cursor=<opaque>&per_page=200``
#
# Two deliberate departures from the portal's behaviour:
#
# * Every sort is stabilised with a trailing ``id ASC``.  The portal's queries have
#   non-deterministic tie order, which under pagination silently skips and repeats
#   rows.
# * Unknown query parameters are rejected with 422 rather than ignored.  Silently
#   ignoring ``?statuss=completed`` would return the whole unfiltered table and look
#   like a data bug on the consumer's side.
# ==========================================================================

import base64
from datetime import datetime

from flask import request

# (merged) now defined in this module: ValidationError
from core.config import (
    DATE_ONLY,
    TimeParseError,
    parse_date,
    parse_date_boundary,
    parse_datetime,
    semantic_for,
)

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 500

OPERATORS = ("in", "ne", "gt", "gte", "lt", "lte", "like", "startswith", "isnull")

#: Accepted by every endpoint; never treated as a filter.
RESERVED_PARAMS = {
    "page",
    "per_page",
    "limit",
    "offset",
    "cursor",
    "sort",
    "fields",
    "expand",
    "q",
    "count_total",
    "strict_params",
    "time_format",
    "include_deprecated",
    "strict_nulls",
    "derived",
    "include_deleted",
    "format",
    "assume_tz",
    # Identity selectors for user-scoped endpoints (core.auth.resolve_actor).
    # They address *who* the request acts as, not what to filter, so they must
    # never be treated as unknown parameters.
    "as_user",
    "manager_email",
    "actor_email",
}

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


def parse_bool(value, field="value"):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValidationError(
        message=f"'{field}' must be a boolean.",
        details=[{"field": field, "issue": "expected true/false", "value": value}],
    )


def parse_int(value, field="value"):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        # The portal does a bare int() here and 500s on bad input.
        raise ValidationError(
            message=f"'{field}' must be an integer.",
            details=[{"field": field, "issue": "expected an integer", "value": value}],
        )


def parse_float(value, field="value"):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(
            message=f"'{field}' must be a number.",
            details=[{"field": field, "issue": "expected a number", "value": value}],
        )


def encode_cursor(value):
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")


def decode_cursor(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        raise ValidationError(
            message="Invalid cursor.",
            details=[{"field": "cursor", "issue": "not a valid cursor", "value": value}],
        )


class ResourceConfig:
    """Describes what a resource allows in its query string."""

    def __init__(
        self,
        model,
        table,
        spec_name,
        sortable=(),
        searchable=(),
        filterable=None,
        sort_presets=None,
        custom_filters=None,
        default_sort="-id",
        default_per_page=DEFAULT_PER_PAGE,
    ):
        self.model = model
        self.table = table
        self.spec_name = spec_name
        self.searchable = tuple(searchable)
        self.sort_presets = sort_presets or {}
        self.custom_filters = custom_filters or {}
        self.default_sort = default_sort
        self.default_per_page = default_per_page

        columns = {attr.key for attr in model.__mapper__.column_attrs}
        self.filterable = set(filterable) if filterable else set(columns)
        self.filterable &= columns
        self.sortable = set(sortable) if sortable else set(columns)
        self.sortable &= columns
        self.columns = columns


class ListParams:
    def __init__(self, config):
        self.config = config
        self.page = 1
        self.per_page = config.default_per_page
        self.cursor = None
        self.sort_raw = None
        self.sort_terms = []
        self.count_total = True
        self.filters_applied = {}

    # -- parsing ---------------------------------------------------------

    @classmethod
    def from_request(cls, config, extra_allowed=()):
        params = cls(config)
        args = request.args

        strict = args.get("strict_params", "true").strip().lower() not in FALSE_VALUES
        allowed = (
            RESERVED_PARAMS
            | set(extra_allowed)
            | set(config.custom_filters)
            | {f"{name}__{op}" for name in config.filterable for op in OPERATORS}
            | set(config.filterable)
        )
        if strict:
            unknown = [key for key in args if key not in allowed]
            if unknown:
                raise ValidationError(
                    message="Unknown query parameter(s).",
                    details=[
                        {
                            "field": key,
                            "issue": "not a recognised filter for this resource",
                        }
                        for key in sorted(unknown)
                    ]
                    + [
                        {
                            "field": "_hint",
                            "issue": "pass strict_params=false to ignore unknown params",
                        }
                    ],
                )

        if args.get("cursor"):
            params.cursor = decode_cursor(args["cursor"])
        else:
            params.page = max(1, parse_int(args.get("page", 1), "page"))

        raw_per_page = args.get("per_page") or args.get("limit")
        if raw_per_page is not None:
            value = parse_int(raw_per_page, "per_page")
            if value < 1:
                raise ValidationError(
                    message="'per_page' must be at least 1.",
                    details=[
                        {
                            "field": "per_page",
                            "issue": "must be >= 1; there is no 'return everything' mode",
                            "value": raw_per_page,
                        }
                    ],
                )
            params.per_page = min(value, MAX_PER_PAGE)

        if args.get("offset") and not args.get("page"):
            offset = parse_int(args["offset"], "offset")
            params.page = (offset // params.per_page) + 1

        params.sort_raw = args.get("sort") or config.default_sort
        params.sort_terms = params._parse_sort(params.sort_raw)
        params.count_total = args.get("count_total", "true").strip().lower() not in FALSE_VALUES
        return params

    def _parse_sort(self, raw):
        config = self.config
        if raw in config.sort_presets:
            return [("__preset__", raw)]

        terms = []
        for piece in str(raw).split(","):
            piece = piece.strip()
            if not piece:
                continue
            descending = piece.startswith("-")
            name = piece[1:] if descending else piece
            if name in config.sort_presets:
                terms.append(("__preset__", name))
                continue
            if name not in config.sortable:
                raise ValidationError(
                    message=f"'{name}' is not sortable on this resource.",
                    details=[
                        {
                            "field": "sort",
                            "issue": "not sortable",
                            "value": name,
                            "allowed": sorted(config.sortable) + sorted(config.sort_presets),
                        }
                    ],
                )
            terms.append((name, "desc" if descending else "asc"))
        return terms

    # -- applying --------------------------------------------------------

    def apply_filters(self, query):
        config = self.config
        model = config.model

        for key, raw in request.args.items(multi=False):
            if key in RESERVED_PARAMS:
                continue

            if key in config.custom_filters:
                query = config.custom_filters[key](query, raw)
                self.filters_applied[key] = raw
                continue

            name, _, operator = key.partition("__")
            if name not in config.filterable:
                continue
            if operator and operator not in OPERATORS:
                continue

            column = getattr(model, name)
            query = self._apply_one(query, column, config.table, name, operator, raw)
            self.filters_applied[key] = raw

        search = request.args.get("q")
        if search and config.searchable:
            from sqlalchemy import or_

            pattern = f"%{search}%"
            query = query.filter(
                or_(*[getattr(model, field).ilike(pattern) for field in config.searchable])
            )
            self.filters_applied["q"] = search

        return query

    def _apply_one(self, query, column, table, name, operator, raw):
        coerce = self._coercer(column, table, name)

        if operator == "isnull":
            return query.filter(column.is_(None) if parse_bool(raw, name) else column.isnot(None))
        if operator == "in":
            values = [coerce(v.strip()) for v in raw.split(",") if v.strip() != ""]
            if not values:
                return query
            return query.filter(column.in_(values))
        if operator == "like":
            return query.filter(column.ilike(f"%{raw}%"))
        if operator == "startswith":
            return query.filter(column.ilike(f"{raw}%"))
        if operator == "ne":
            return query.filter(column != coerce(raw))
        if operator == "gt":
            return query.filter(column > coerce(raw, end=False))
        if operator == "gte":
            return query.filter(column >= coerce(raw, end=False))
        if operator == "lt":
            return query.filter(column < coerce(raw, end=False))
        if operator == "lte":
            return query.filter(column <= coerce(raw, end=True))
        return query.filter(column == coerce(raw))

    def _coercer(self, column, table, name):
        from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric

        column_type = column.type

        def coerce(value, end=False):
            if value == "" or value is None:
                return None
            if isinstance(column_type, Boolean):
                return parse_bool(value, name)
            if isinstance(column_type, Integer):
                return parse_int(value, name)
            if isinstance(column_type, (Float, Numeric)):
                return parse_float(value, name)
            if isinstance(column_type, DateTime):
                semantic = semantic_for(table, name)
                try:
                    if len(str(value).strip()) == 10:  # a bare YYYY-MM-DD
                        return parse_date_boundary(value, semantic, end=end)
                    return parse_datetime(value, semantic)
                except TimeParseError as exc:
                    raise ValidationError(
                        message=str(exc),
                        details=[{"field": name, "issue": "invalid datetime", "value": value}],
                    )
            if isinstance(column_type, Date):
                try:
                    return parse_date(value)
                except TimeParseError as exc:
                    raise ValidationError(
                        message=str(exc),
                        details=[{"field": name, "issue": "invalid date", "value": value}],
                    )
            return value

        _ = DATE_ONLY
        return coerce

    def apply_sort(self, query):
        config = self.config
        model = config.model
        clauses = []

        for name, direction in self.sort_terms:
            if name == "__preset__":
                preset = config.sort_presets[direction]
                built = preset() if callable(preset) else preset
                if isinstance(built, (list, tuple)):
                    clauses.extend(built)
                else:
                    clauses.append(built)
                continue
            column = getattr(model, name)
            clauses.append(column.desc() if direction == "desc" else column.asc())

        # Stabiliser: without it, equal sort keys give a non-deterministic order
        # and pagination drops/duplicates rows between pages.
        clauses.append(model.id.asc())
        return query.order_by(*clauses)

    def apply(self, query):
        return self.apply_sort(self.apply_filters(query))

    # -- paginating ------------------------------------------------------

    def paginate(self, query):
        model = self.config.model

        if self.cursor is not None:
            rows = (
                query.filter(model.id > self.cursor)
                .order_by(model.id.asc())
                .limit(self.per_page + 1)
                .all()
            )
            has_next = len(rows) > self.per_page
            rows = rows[: self.per_page]
            return rows, {
                "page": 1,
                "per_page": self.per_page,
                "total": None,
                "total_pages": None,
                "has_next": has_next,
                "cursor_mode": True,
                "next_cursor": encode_cursor(rows[-1].id) if rows and has_next else None,
            }

        total = query.count() if self.count_total else None
        offset = (self.page - 1) * self.per_page
        rows = query.limit(self.per_page + 1).offset(offset).all()
        has_next = len(rows) > self.per_page
        rows = rows[: self.per_page]

        total_pages = None
        if total is not None:
            total_pages = max(1, (total + self.per_page - 1) // self.per_page)

        return rows, {
            "page": self.page,
            "per_page": self.per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": has_next,
            "cursor_mode": False,
        }

    def meta(self):
        return {"filters_applied": self.filters_applied, "sort": self.sort_raw}


# ---------------------------------------------------------------------------
# Survey-specific compound filters
# ---------------------------------------------------------------------------
# These expand to several clauses, so they cannot be expressed as plain
# column comparisons. Semantics are copied from routes/admin.py.


def survey_custom_filters():
    from sqlalchemy import or_

    from models.db_models import Survey

    from core.config import states_for_team
    from core.config import week_window

    def by_week(query, value):
        start, end = week_window(parse_int(value, "week"))
        return query.filter(Survey.start_time >= start, Survey.start_time < end)

    def by_from_date(query, value):
        boundary = parse_date_boundary(value, semantic_for("surveys", "start_time"))
        return query.filter(Survey.start_time >= boundary)

    def by_to_date(query, value):
        # Exclusive upper bound at midnight of the next day - admin.py:167.
        boundary = parse_date_boundary(value, semantic_for("surveys", "start_time"), end=True)
        return query.filter(Survey.start_time < boundary)

    def by_survey_date(query, value):
        # Single-day window used by teamleader.py / roadvision.py.
        start = parse_date_boundary(value, semantic_for("surveys", "start_time"))
        end = parse_date_boundary(value, semantic_for("surveys", "start_time"), end=True)
        return query.filter(Survey.start_time >= start, Survey.start_time < end)

    def by_team(query, value):
        states = states_for_team(value)
        if not states:
            raise ValidationError(
                message=f"Unknown team '{value}'.",
                details=[
                    {"field": "team", "issue": "unknown", "value": value,
                     "allowed": ["Krish", "Godbole", "Aspizo"]}
                ],
            )
        return query.filter(Survey.state.in_(list(states)))

    def by_stretch(query, value):
        return query.filter(Survey.stretch_code.ilike(f"%{value}%"))

    def by_has_pdf(query, value):
        return query.filter(
            Survey.end_survey_pdf.isnot(None)
            if parse_bool(value, "has_pdf")
            else Survey.end_survey_pdf.is_(None)
        )

    def by_has_photo(query, value):
        return query.filter(
            Survey.dashcam_photo.isnot(None)
            if parse_bool(value, "has_dashcam_photo")
            else Survey.dashcam_photo.is_(None)
        )

    def by_pending_task(query, value):
        if not parse_bool(value, "has_pending_task"):
            return query.filter(
                Survey.task1_completed.is_(True),
                Survey.task2_completed.is_(True),
                Survey.survey_form_completed.is_(True),
            )
        return query.filter(
            or_(
                Survey.task1_completed.isnot(True),
                Survey.task2_completed.isnot(True),
                Survey.survey_form_completed.isnot(True),
            )
        )

    return {
        "week": by_week,
        "from_date": by_from_date,
        "to_date": by_to_date,
        "survey_date": by_survey_date,
        "team": by_team,
        "stretch": by_stretch,
        "has_pdf": by_has_pdf,
        "has_dashcam_photo": by_has_photo,
        "has_pending_task": by_pending_task,
    }


def assignment_custom_filters():
    from models.db_models import SurveyAssignment

    from core.config import states_for_team

    def by_team(query, value):
        states = states_for_team(value)
        if not states:
            raise ValidationError(
                message=f"Unknown team '{value}'.",
                details=[{"field": "team", "issue": "unknown", "value": value}],
            )
        return query.filter(SurveyAssignment.state.in_(list(states)))

    def by_stretch(query, value):
        return query.filter(SurveyAssignment.stretch_code.ilike(f"%{value}%"))

    def by_has_reason(query, value):
        return query.filter(
            SurveyAssignment.missed_reason.isnot(None)
            if parse_bool(value, "has_missed_reason")
            else SurveyAssignment.missed_reason.is_(None)
        )

    return {"team": by_team, "stretch": by_stretch, "has_missed_reason": by_has_reason}


_ = datetime  # re-exported for callers that build their own boundaries

# ==========================================================================
# serializers
# Model -> JSON.
#
# Column introspection rather than hand-written field lists, so a column added to
# models/db_models.py appears in the API automatically. (That is how
# ``video_count_matched`` / ``video_count_checked_at`` are already covered.)
#
# Field naming is deliberately 1:1 with the database - ``section_no`` stays
# ``section_no``.  The consuming site maps these onto its own schema once; a
# faithful name match is the least error-prone contract.
#
# Datetimes expand into three keys - ``<field>_utc``, ``<field>_ist``,
# ``<field>_raw`` - because the columns disagree about what they store.  See
# core/timeutils.py.
# ==========================================================================

# Aliased: other sections do `from datetime import datetime`, which would
# otherwise shadow the module and break the isinstance checks below.
import datetime as _datetime
import decimal
import uuid

from sqlalchemy import inspect as sa_inspect

from core.config import DEAD_SURVEY_COLUMNS
from core.config import DATE_ONLY, semantic_for, serialize_datetime

#: Never emitted, under any option, from any endpoint.
GLOBAL_DENYLIST = {"password_hash"}

_COLUMN_CACHE = {}


def jsonify_value(value):
    """Convert a Python/SQLAlchemy scalar into something json.dumps can handle."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, _datetime.datetime):
        return value.isoformat()
    if isinstance(value, _datetime.date):
        return value.isoformat()
    if isinstance(value, _datetime.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [jsonify_value(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonify_value(item) for key, item in value.items()}
    return str(value)


def columns_of(model):
    """Cached ``[(attr_name, Column)]`` for a mapped class."""
    key = model.__name__
    if key not in _COLUMN_CACHE:
        mapper = sa_inspect(model).mapper
        _COLUMN_CACHE[key] = [
            (attr.key, attr.columns[0]) for attr in mapper.column_attrs
        ]
    return _COLUMN_CACHE[key]


class Options:
    """Per-request serialization switches, parsed from the query string."""

    def __init__(
        self,
        fields=None,
        include_deprecated=False,
        strict_nulls=False,
        time_format="flat",
        derived=False,
    ):
        self.fields = set(fields) if fields else None
        self.include_deprecated = include_deprecated
        self.strict_nulls = strict_nulls
        self.time_format = time_format  # flat | object | iso
        self.derived = derived

    @classmethod
    def from_request(cls, default_derived=False):
        from flask import request

        raw_fields = request.args.get("fields")
        fields = (
            [f.strip() for f in raw_fields.split(",") if f.strip()] if raw_fields else None
        )

        def flag(name, default=False):
            value = request.args.get(name)
            if value is None:
                return default
            return value.strip().lower() in ("1", "true", "yes")

        time_format = (request.args.get("time_format") or "flat").lower()
        if time_format not in ("flat", "object", "iso"):
            time_format = "flat"

        return cls(
            fields=fields,
            include_deprecated=flag("include_deprecated"),
            strict_nulls=flag("strict_nulls"),
            time_format=time_format,
            derived=flag("derived", default_derived),
        )


DEFAULT_OPTIONS = Options()


class FieldSpec:
    """
    How one model is serialized.

    ``coerce_false`` / ``coerce_zero`` reproduce the portal's behaviour: every
    ``if survey.task1_completed:`` in the codebase treats NULL as False, so the
    API emits False rather than null.  ``?strict_nulls=true`` shows the truth.
    """

    def __init__(
        self,
        model,
        table,
        exclude=(),
        deprecated=(),
        computed=None,
        read_only=("id",),
        not_writable=(),
        coerce_false=(),
        coerce_zero=(),
        never_coerce=(),
    ):
        self.model = model
        self.table = table
        self.exclude = set(exclude) | GLOBAL_DENYLIST
        self.deprecated = set(deprecated)
        self.computed = computed or {}
        self.read_only = set(read_only)
        self.not_writable = set(not_writable) | self.read_only | GLOBAL_DENYLIST
        self.coerce_false = set(coerce_false)
        self.coerce_zero = set(coerce_zero)
        self.never_coerce = set(never_coerce)

    # -- reading ---------------------------------------------------------

    def visible_columns(self, options):
        for name, column in columns_of(self.model):
            if name in self.exclude:
                continue
            if name in self.deprecated and not options.include_deprecated:
                continue
            yield name, column

    def serialize(self, obj, options=None, extra=None):
        options = options or DEFAULT_OPTIONS
        data = {}

        for name, column in self.visible_columns(options):
            value = getattr(obj, name, None)

            if _is_temporal(column):
                self._emit_temporal(data, name, value, options)
                continue

            if value is None and not options.strict_nulls and name not in self.never_coerce:
                if name in self.coerce_false:
                    value = False
                elif name in self.coerce_zero:
                    value = 0

            if value is not None and _is_float(column):
                value = float(value)

            data[name] = jsonify_value(value)

        for name, func in self.computed.items():
            try:
                data[name] = jsonify_value(func(obj))
            except Exception:
                data[name] = None

        if extra:
            data.update({key: jsonify_value(value) for key, value in extra.items()})

        if options.fields:
            keep = options.fields
            data = {
                key: value
                for key, value in data.items()
                # keep foo_utc/_ist/_raw when the caller asked for "foo"
                if key in keep or key.rsplit("_", 1)[0] in keep
            }

        return data

    def _emit_temporal(self, data, name, value, options):
        semantic = semantic_for(self.table, name)

        if semantic == DATE_ONLY:
            data[name] = value.isoformat() if value else None
            return

        parts = serialize_datetime(value, semantic)

        if options.time_format == "iso":
            data[name] = parts["utc"]
        elif options.time_format == "object":
            data[name] = dict(parts, semantic=semantic) if value else None
        else:
            data[f"{name}_utc"] = parts["utc"]
            data[f"{name}_ist"] = parts["ist"]
            data[f"{name}_raw"] = parts["raw"]

    # -- writing ---------------------------------------------------------

    def writable_columns(self):
        return [
            (name, column)
            for name, column in columns_of(self.model)
            if name not in self.not_writable
        ]

    def is_writable(self, name):
        return name in {n for n, _ in self.writable_columns()}


def _is_temporal(column):
    from sqlalchemy import Date, DateTime, Time

    return isinstance(column.type, (DateTime, Date, Time))


def _is_float(column):
    from sqlalchemy import Float, Numeric

    return isinstance(column.type, (Float, Numeric))


def serialize_many(objects, spec, options=None, extra_by_id=None):
    options = options or DEFAULT_OPTIONS
    result = []
    for obj in objects:
        extra = None
        if extra_by_id:
            extra = extra_by_id.get(getattr(obj, "id", None))
        result.append(spec.serialize(obj, options, extra))
    return result


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

_SPECS = {}


def _build_specs():
    from models.db_models import (
        Equipment,
        RegionalManagerState,
        Survey,
        SurveyAssignment,
        SurveySchedule,
        User,
    )

    from core.config import DEFECT_COUNT_FIELDS
    from core.config import survey_ref_id
    from core.config import team_for_state, team_display
    from core.config import week_of

    survey_bools = (
        "pdf_reupload_required",
        "video_uploaded",
        "is_resurvey",
        "resurvey_requested",
        "resurvey_approved",
        "task1_completed",
        "task2_completed",
        "survey_form_completed",
        "show_on_dashboard",
        "show_in_teamleader_dashboard",
        "roadvision_completed",
    )

    specs = {
        "survey": FieldSpec(
            model=Survey,
            table="surveys",
            deprecated=DEAD_SURVEY_COLUMNS,
            coerce_false=survey_bools,
            coerce_zero=DEFECT_COUNT_FIELDS + ("pdf_reupload_count", "cycle_no"),
            # admin.py:268 and 875 do `or 0`, but on a raw record a missing
            # length is genuinely unknown - don't invent a zero here.
            never_coerce=("section_length", "video_count_matched"),
            computed={
                "team": lambda s: team_for_state(s.state),
                "team_display": lambda s: team_display(team_for_state(s.state))
                if team_for_state(s.state)
                else None,
                "week_no": lambda s: week_of(s.start_time),
                "survey_ref_id": lambda s: survey_ref_id(s) if s.upc_code else None,
            },
        ),
        "assignment": FieldSpec(
            model=SurveyAssignment,
            table="survey_assignments",
            coerce_false=(
                "survey_enabled",
                "alert_acknowledged",
                "missed_alert",
                "alert_generated",
            ),
            never_coerce=("section_length",),
            computed={
                "team": lambda a: team_for_state(a.state),
            },
        ),
        "user": FieldSpec(model=User, table="users"),
        "schedule": FieldSpec(model=SurveySchedule, table="survey_schedule"),
        "equipment": FieldSpec(model=Equipment, table="equipment"),
        "manager_state": FieldSpec(
            model=RegionalManagerState, table="regional_manager_states"
        ),
    }
    return specs


def spec(name):
    """Look up a FieldSpec by resource name, building them on first use."""
    global _SPECS
    if not _SPECS:
        _SPECS = _build_specs()
    return _SPECS[name]


def survey_derived(survey, now=None):
    """
    The computed block for ``GET /surveys/{id}/derived``.

    These values are what the portal calculates at render time; none of them are
    columns.
    """
    from core.config import (
        defect_counts,
        is_upload_overdue,
        survey_duration_minutes,
        upload_duration,
    )
    from core.config import iso_naive, legacy_display

    minutes, text = upload_duration(survey, now)
    return {
        "survey_duration_minutes": survey_duration_minutes(survey),
        "upload_duration_minutes": minutes,
        "upload_status_text": text,
        "upload_is_overdue": is_upload_overdue(minutes),
        "defect_counts": defect_counts(survey),
        # Reproduces the portal's on-screen value, +5:30 bug included.
        "display_start_time": iso_naive(legacy_display(survey.start_time)),
        "display_end_time": iso_naive(legacy_display(survey.end_time)),
    }

# ==========================================================================
# crud
# Generic REST behaviour shared by every resource.
#
# list / get / create / update / delete are identical apart from the model and a
# few rules, so they live here once.  Resource modules supply a
# :class:`~core.params.ResourceConfig` and any hooks they need.
#
# Soft delete writes a tombstone to ``api_deleted_records`` instead of removing the
# row - see core.api_models.DeletedRecord for why it is a side table rather than a
# ``deleted_at`` column.  Hard deletion is genuinely unsafe on this schema:
# ``cycle_no`` is derived by scanning survey history (routes/captain.py:228-235),
# so removing the newest survey rewinds the cycle sequence and the next survey
# silently reuses a number that already appears in exported reports and PDF
# filenames.
# ==========================================================================

from flask import request
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

# (merged) now defined in this module: created, no_content, ok, paginated
# (merged) now defined in this module: BadRequest, Conflict, NotFound, ValidationError, translate_db_error
# (merged) now defined in this module: ListParams, parse_bool
# (merged) now defined in this module: Options, serialize_many, spec
from core.config import TimeParseError, parse_date, parse_datetime, semantic_for


#: model class name -> the entity slug used in api_deleted_records.
ENTITY_FOR_MODEL = {
    "Survey": "survey",
    "SurveyAssignment": "assignment",
    "User": "user",
    "SurveySchedule": "schedule",
    "Equipment": "equipment",
    "RegionalManagerState": "manager_state",
}


def entity_of(model):
    return ENTITY_FOR_MODEL.get(model.__name__, model.__name__.lower())


def deleted_ids(model):
    """Ids tombstoned for this model. Empty set if the table isn't created yet."""
    from core.models import DeletedRecord

    try:
        rows = (
            DeletedRecord.query.with_entities(DeletedRecord.entity_id)
            .filter(DeletedRecord.entity == entity_of(model))
            .all()
        )
        return {row[0] for row in rows}
    except Exception:
        from extensions import db

        db.session.rollback()
        return set()


def base_query(model, include_deleted=False):
    """Query excluding soft-deleted rows unless explicitly asked for."""
    query = model.query
    if not include_deleted:
        from core.models import DeletedRecord

        try:
            tombstones = DeletedRecord.query.with_entities(DeletedRecord.entity_id).filter(
                DeletedRecord.entity == entity_of(model)
            )
            query = query.filter(~model.id.in_(tombstones))
        except Exception:
            from extensions import db

            db.session.rollback()
    return query


def wants_deleted():
    return parse_bool(request.args.get("include_deleted", "false"), "include_deleted")


def get_or_404(model, identifier, include_deleted=False):
    row = base_query(model, include_deleted).filter(model.id == identifier).first()
    if row is None:
        raise NotFound(message=f"{model.__name__} {identifier} not found.")
    return row


def list_resource(config, query=None, extra_allowed=(), transform=None, default_derived=False):
    """Standard list endpoint: filter -> sort -> paginate -> serialize."""
    params = ListParams.from_request(config, extra_allowed=extra_allowed)
    query = query if query is not None else base_query(config.model, wants_deleted())
    query = params.apply(query)
    rows, page_info = params.paginate(query)

    if transform:
        transform(rows)

    options = Options.from_request(default_derived=default_derived)
    items = serialize_many(rows, spec(config.spec_name), options)
    return paginated(items, page_info, meta=params.meta())


def show_resource(config, identifier, extra=None):
    row = get_or_404(config.model, identifier, wants_deleted())
    options = Options.from_request()
    return ok(spec(config.spec_name).serialize(row, options, extra(row) if extra else None))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def json_body(required=True):
    if request.content_type and "json" not in (request.content_type or "").lower():
        from core.engine import UnsupportedMediaType

        raise UnsupportedMediaType(
            message="Send this request as application/json.",
            details=[{"field": "Content-Type", "issue": "expected application/json"}],
        )
    body = request.get_json(silent=True)
    if body is None:
        if required:
            raise BadRequest(message="A JSON object body is required.")
        return {}
    if not isinstance(body, dict):
        raise BadRequest(message="Request body must be a JSON object.")
    return body


def coerce_field(model, table, name, value):
    """Convert one incoming JSON value to what the column expects."""
    from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric

    column = model.__table__.columns.get(name)
    if column is None or value is None:
        return value
    column_type = column.type

    try:
        if isinstance(column_type, Boolean):
            return parse_bool(value, name)
        if isinstance(column_type, Integer):
            return int(value)
        if isinstance(column_type, (Float, Numeric)):
            return float(value)
        if isinstance(column_type, DateTime):
            return parse_datetime(
                value, semantic_for(table, name), assume_tz=request.args.get("assume_tz")
            )
        if isinstance(column_type, Date):
            return parse_date(value)
    except TimeParseError as exc:
        raise ValidationError(
            message=str(exc),
            details=[{"field": name, "issue": "invalid datetime", "value": value}],
        )
    except (TypeError, ValueError):
        raise ValidationError(
            message=f"'{name}' has the wrong type for its column.",
            details=[{"field": name, "issue": "wrong type", "value": value}],
        )
    return value


def apply_body(row, body, config, partial=True, forbidden=()):
    """
    Copy validated fields from the request body onto a model instance.

    Unknown or read-only keys are a 422, not a silent no-op - a consumer typo
    should be loud.  A key present with a ``null`` value sets the column to NULL;
    to leave a field untouched, omit it.
    """
    field_spec = spec(config.spec_name)
    writable = {name for name, _ in field_spec.writable_columns()}
    blocked = set(forbidden)

    unknown = [key for key in body if key not in config.columns]
    read_only = [key for key in body if key in config.columns and key not in writable]
    protected = [key for key in body if key in blocked]

    problems = []
    for key in sorted(unknown):
        problems.append({"field": key, "issue": "not a field on this resource"})
    for key in sorted(read_only):
        problems.append({"field": key, "issue": "read-only"})
    for key in sorted(protected):
        problems.append(
            {
                "field": key,
                "issue": "cannot be set directly; use the dedicated action endpoint",
            }
        )
    if problems:
        raise ValidationError(message="Request body failed validation.", details=problems)

    if not partial:
        # PUT semantics: everything writable and absent becomes NULL.
        for name in writable:
            if name not in body:
                setattr(row, name, None)

    for key, value in body.items():
        setattr(row, key, coerce_field(config.model, config.table, key, value))

    return row


def commit(action="save"):
    from extensions import db

    try:
        db.session.commit()
    except (IntegrityError, DataError) as exc:
        db.session.rollback()
        raise translate_db_error(exc)
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise translate_db_error(exc)
    _ = action


def create_resource(config, location_prefix, required=(), forbidden=(), defaults=None):
    from extensions import db

    body = json_body()
    for field in required:
        if body.get(field) in (None, ""):
            raise ValidationError(
                message=f"'{field}' is required.",
                details=[{"field": field, "issue": "required"}],
            )

    row = config.model()
    for key, value in (defaults or {}).items():
        setattr(row, key, value)
    apply_body(row, body, config, partial=True, forbidden=forbidden)

    db.session.add(row)
    commit("create")

    data = spec(config.spec_name).serialize(row, Options.from_request())
    return created(data, location=f"{location_prefix}/{row.id}")


def update_resource(config, identifier, partial=True, forbidden=()):
    row = get_or_404(config.model, identifier, wants_deleted())
    body = json_body()
    apply_body(row, body, config, partial=partial, forbidden=forbidden)
    commit("update")
    return ok(spec(config.spec_name).serialize(row, Options.from_request()))


def delete_resource(config, identifier, on_soft_delete=None, allow_hard=True):
    """
    Soft delete by default; ``?hard=true`` needs the ``admin:destroy`` scope.

    Hard delete also refuses when other rows still reference the row's business
    key, because this schema has no foreign keys to protect it.
    """
    from extensions import db

    from core.models import DeletedRecord
    from core.engine import current_client

    row = get_or_404(config.model, identifier, include_deleted=True)
    hard = parse_bool(request.args.get("hard", "false"), "hard")
    client = current_client()

    if hard:
        if not allow_hard:
            raise Conflict(
                message=f"{config.model.__name__} does not support hard deletion.",
                code="hard_delete_forbidden",
            )
        if client:
            client.require("admin:destroy")
        _guard_references(config, row)
        db.session.delete(row)
        commit("delete")
        return no_content()

    entity = entity_of(config.model)
    existing = DeletedRecord.query.filter_by(entity=entity, entity_id=row.id).first()
    if existing:
        return no_content()  # already deleted - idempotent

    db.session.add(
        DeletedRecord(
            entity=entity,
            entity_id=row.id,
            deleted_by=client.subject if client else None,
            reason=request.args.get("reason"),
        )
    )

    # Also hide it in the portal where a flag exists to do so. This covers
    # /admin, /regional and /teamleader; the roadvision and captain lists filter
    # on the tombstone directly via utils.visibility.
    if on_soft_delete:
        on_soft_delete(row)
    else:
        for flag in ("show_on_dashboard", "show_in_teamleader_dashboard"):
            if hasattr(row, flag):
                setattr(row, flag, False)

    # Tell change capture this is a deletion, not an ordinary field update.
    # The tombstone lives in a side table that nothing listens to, and the row
    # itself only shows `show_on_dashboard: true -> false`, so without this the
    # `*.deleted` event never fired for a soft delete - the consumer saw a
    # meaningless `*.updated` and kept the record alive on its side.
    row._viama_soft_deleted = True

    commit("delete")
    return no_content()


def restore_resource(config, identifier):
    """Undo a soft delete."""
    from extensions import db

    from core.models import DeletedRecord

    row = get_or_404(config.model, identifier, include_deleted=True)
    tombstone = DeletedRecord.query.filter_by(
        entity=entity_of(config.model), entity_id=row.id
    ).first()
    if not tombstone:
        raise Conflict(
            message=f"{config.model.__name__} {identifier} is not deleted.",
            code="not_deleted",
        )
    db.session.delete(tombstone)
    for flag in ("show_on_dashboard", "show_in_teamleader_dashboard"):
        if hasattr(row, flag):
            setattr(row, flag, True)
    commit("restore")
    return ok(spec(config.spec_name).serialize(row, Options.from_request()))


def _guard_references(config, row):
    """Refuse a hard delete that would orphan rows joined by string key."""
    from models.db_models import Survey, SurveyAssignment

    model_name = config.model.__name__

    if model_name == "User" and row.email:
        blockers = {
            "surveys": Survey.query.filter_by(captain_email=row.email).count(),
            "assignments": SurveyAssignment.query.filter_by(captain_email=row.email).count(),
        }
    elif model_name == "SurveyAssignment" and row.section_no:
        blockers = {"surveys": Survey.query.filter_by(section_no=row.section_no).count()}
    else:
        return

    outstanding = {key: count for key, count in blockers.items() if count}
    if outstanding:
        raise Conflict(
            message=(
                "Other rows still reference this record by string key, and this "
                "schema has no foreign keys to clean them up. Soft-delete instead."
            ),
            code="referenced_by_other_rows",
            details=[
                {"field": key, "issue": "still referencing", "value": count}
                for key, count in outstanding.items()
            ],
        )

# ==========================================================================
# svc_survey
# The survey workflow, as a service.
#
# Ported from routes/captain.py, routes/admin.py, routes/teamleader.py and
# routes/roadvision.py.  The portal keeps this logic inline in view functions and
# carries state in the Flask session; here it is explicit and stateless so a
# machine client can drive it.
#
# What is added on top of the portal's behaviour:
#
# * A transition validator.  The portal has none - it just assigns ``status``.
# * Row locking, and an advisory lock around survey creation.  The portal's
#   duplicate guard and ``cycle_no`` derivation are a read-then-write race that two
#   captains submitting at once can lose (routes/captain.py:193-235).
# * One-shot guards enforced server-side.  In the portal, "already reviewed" is
#   only prevented by the UI not offering the button.
# ==========================================================================

from datetime import datetime

import pytz

from core.config import (
    ALLOWED_STATUS_TRANSITIONS,
    DEFECT_COUNT_FIELDS,
    IST_TZ_NAME,
    IN_PROGRESS_STATUSES,
    SURVEY_CANCELLED,
    SURVEY_COMPLETED,
    SURVEY_GROUNDWORK_COMPLETED,
    SURVEY_ONGOING,
    SURVEY_VIDEO_PENDING,
    TASK_FIELDS,
    ACTIVE_SURVEY_STATUSES,
)
# (merged) now defined in this module: Conflict, InvalidTransition, NotFound, ValidationError
from core.config import utc_now, week_start_sunday


def ist_aware_now():
    """
    How the portal stamps ``start_time`` / ``end_time``.

    Deliberately tz-aware Asia/Kolkata written into a naive column, matching
    routes/captain.py:271 and :538 - so the value on disk is IST wall-clock.
    Changing this here would make API-created surveys disagree with portal-created
    ones by 5h30m.
    """
    return datetime.now(pytz.timezone(IST_TZ_NAME))


def assert_transition(survey, target):
    current = survey.status or SURVEY_ONGOING
    if current == target:
        raise Conflict(
            message=f"Survey {survey.id} is already '{target}'.",
            code="already_in_state",
        )
    allowed = ALLOWED_STATUS_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(current, target, allowed)


def lock_survey(survey_id):
    """
    Load a survey FOR UPDATE.

    Without this, two concurrent ``complete`` calls both read
    ``groundwork_completed`` and both write.  Falls back to a plain get on SQLite,
    which has no row locking.
    """
    from extensions import db
    from models.db_models import Survey

    try:
        survey = db.session.get(Survey, survey_id, with_for_update=True)
    except Exception:
        survey = db.session.get(Survey, survey_id)
    if not survey:
        raise NotFound(message=f"Survey {survey_id} not found.")
    return survey


def _advisory_lock(key):
    """
    Serialise survey creation per section_no.

    ``FOR UPDATE`` cannot help here: the contended resource is a row that does
    not exist yet.  A transaction-scoped advisory lock closes both the duplicate
    survey and the duplicate cycle_no races, and auto-releases if the process
    dies.  No-op on SQLite.
    """
    from sqlalchemy import text

    from extensions import db

    try:
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(key)}
        )
    except Exception:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_survey(
    assignment,
    captain,
    survey_type,
    survey_day=None,
    dashcam_photo=None,
    is_resurvey=False,
    actor_role=None,
    force=False,
):
    """
    Create a survey - the API equivalent of submitting the checklist.

    Port of routes/captain.py:99-295, including the Sunday-week duplicate guard
    and the approved-resurvey consumption.
    """
    from extensions import db
    from models.db_models import Survey

    _advisory_lock(assignment.section_no)

    # --- weekly duplicate guard (Sunday-start week; the alert engine uses
    #     Monday - the two genuinely differ, see core.timeutils) ---
    week_start = week_start_sunday(utc_now())
    existing = (
        Survey.query.filter(
            Survey.section_no == assignment.section_no,
            Survey.start_time >= week_start,
            Survey.status.in_(list(ACTIVE_SURVEY_STATUSES)),
        )
        .order_by(Survey.id.desc())
        .first()
    )

    if existing and not existing.resurvey_approved and not force:
        raise Conflict(
            message="A survey for this section has already been started this week.",
            code="survey_already_started",
            details=[
                {
                    "field": "section_no",
                    "issue": "already surveyed this week",
                    "value": assignment.section_no,
                    "survey_id": existing.id,
                    "captain_name": existing.captain_name,
                    "status": existing.status,
                }
            ],
        )

    # --- cycle number ---
    cycle_no = 1
    latest = (
        Survey.query.filter(Survey.section_no == assignment.section_no)
        .order_by(Survey.id.desc())
        .first()
    )
    if latest:
        cycle_no = (latest.cycle_no or 0) + 1

    if not is_resurvey:
        approved = (
            Survey.query.filter(
                Survey.section_no == assignment.section_no,
                Survey.resurvey_approved.is_(True),
            )
            .order_by(Survey.id.desc())
            .first()
        )
        if approved:
            # An approved re-survey reuses the cycle number and consumes the approval.
            cycle_no = approved.cycle_no
            is_resurvey = True
            approved.resurvey_approved = False

    survey = Survey(
        captain_email=captain.email,
        captain_name=captain.name,
        state=assignment.state,
        stretch_code=assignment.stretch_code,
        section_no=assignment.section_no,
        upc_code=assignment.upc_code,
        nh_number=assignment.nh_number,
        ro=assignment.ro,
        piu=assignment.piu,
        survey_day=survey_day or assignment.survey_day,
        survey_type=survey_type,
        section_length=assignment.section_length,
        status=SURVEY_ONGOING,
        start_time=ist_aware_now(),
        dashcam_photo=dashcam_photo,
        cycle_no=cycle_no,
        is_resurvey=is_resurvey,
    )
    db.session.add(survey)

    if assignment.status == "missed":
        assignment.alert_acknowledged = False
    assignment.status = (
        "backup_in_progress" if actor_role == "backup_captain" else "started"
    )

    db.session.commit()
    return survey


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def groundwork_complete(survey_id):
    """routes/captain.py:367-382"""
    from extensions import db

    survey = lock_survey(survey_id)
    assert_transition(survey, SURVEY_GROUNDWORK_COMPLETED)
    survey.status = SURVEY_GROUNDWORK_COMPLETED
    db.session.commit()
    return survey


def complete_survey(survey_id, pdf_url, actor_role=None):
    """
    Finish the field work - routes/captain.py:465-564.

    Note the mixed conventions, preserved on purpose: ``end_time`` is IST
    wall-clock while ``video_pending_start_time`` is UTC.
    """
    from extensions import db
    from models.db_models import SurveyAssignment

    survey = lock_survey(survey_id)
    assert_transition(survey, SURVEY_VIDEO_PENDING)

    if not pdf_url:
        raise ValidationError(
            message="A survey PDF is required to complete a survey.",
            details=[{"field": "pdf_url", "issue": "required"}],
        )

    survey.end_survey_pdf = pdf_url
    survey.video_uploaded = False
    survey.status = SURVEY_VIDEO_PENDING
    survey.end_time = ist_aware_now()
    survey.video_pending_start_time = utc_now()

    assignment = (
        SurveyAssignment.query.filter_by(section_no=survey.section_no)
        .order_by(SurveyAssignment.id)
        .first()
    )
    if assignment:
        assignment.status = (
            "completed_by_backup" if actor_role == "backup_captain" else "completed"
        )

    db.session.commit()
    return survey


def submit_video_counts(survey_id, counts):
    """
    Record the 8 defect counts and finish - routes/captain.py:695-713.

    The portal and the API disagree on purpose about what to do with a bad
    count. The portal used to do a bare ``int(request.form.get(...))`` and 500;
    it now coerces through ``utils.request_params.safe_count``, so garbage and
    negatives silently become 0 rather than breaking a captain's submission
    mid-flow. An API client gets the stricter treatment - the values are
    validated here and a bad one is rejected with the offending field named,
    because a caller sending garbage wants to be told, not quietly corrected.
    """
    from extensions import db

    survey = lock_survey(survey_id)
    assert_transition(survey, SURVEY_COMPLETED)

    problems = []
    cleaned = {}
    for field in DEFECT_COUNT_FIELDS:
        raw = counts.get(field, 0)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            problems.append({"field": field, "issue": "must be an integer", "value": raw})
            continue
        if value < 0:
            problems.append({"field": field, "issue": "must be >= 0", "value": raw})
            continue
        cleaned[field] = value

    unknown = [key for key in counts if key not in DEFECT_COUNT_FIELDS]
    problems.extend({"field": key, "issue": "not a defect count field"} for key in unknown)

    if problems:
        raise ValidationError(message="Video counts failed validation.", details=problems)

    for field, value in cleaned.items():
        setattr(survey, field, value)

    survey.video_uploaded = True
    survey.video_upload_time = utc_now()
    survey.status = SURVEY_COMPLETED

    db.session.commit()
    return survey


def cancel_survey(survey_id, reason=None):
    """
    Abort a stuck survey.

    Not a portal feature.  Safe to add because every portal predicate uses a
    positive ``status.in_([...])`` list, so a cancelled survey simply stops
    matching and no template needed changing.
    """
    from extensions import db

    survey = lock_survey(survey_id)
    assert_transition(survey, SURVEY_CANCELLED)
    survey.status = SURVEY_CANCELLED
    survey.show_on_dashboard = False
    survey.show_in_teamleader_dashboard = False
    if reason:
        note = f"[cancelled] {reason}"
        survey.roadvision_remark = (
            f"{survey.roadvision_remark}\n{note}" if survey.roadvision_remark else note
        )
    db.session.commit()
    return survey


# ---------------------------------------------------------------------------
# Re-survey
# ---------------------------------------------------------------------------


def request_resurvey(survey_id):
    """routes/captain.py:857-869"""
    from extensions import db

    survey = lock_survey(survey_id)
    if survey.resurvey_requested:
        raise Conflict(
            message="A re-survey has already been requested for this survey.",
            code="already_requested",
        )
    survey.resurvey_requested = True
    db.session.commit()
    return survey


def approve_resurvey(survey_id):
    """routes/admin.py:649-662"""
    from extensions import db

    survey = lock_survey(survey_id)
    if survey.resurvey_approved:
        raise Conflict(message="Already approved.", code="already_approved")
    survey.resurvey_approved = True
    db.session.commit()
    return survey


def reject_resurvey(survey_id):
    """Not in the portal - the counterpart to approve, so a request can be closed."""
    from extensions import db

    survey = lock_survey(survey_id)
    if not survey.resurvey_requested:
        raise Conflict(message="No re-survey request is pending.", code="not_requested")
    survey.resurvey_requested = False
    survey.resurvey_approved = False
    db.session.commit()
    return survey


# ---------------------------------------------------------------------------
# PDF re-upload
# ---------------------------------------------------------------------------


def request_pdf_reupload(survey_id, reason):
    """routes/admin.py:743-758"""
    from extensions import db

    survey = lock_survey(survey_id)
    if not (reason or "").strip():
        raise ValidationError(
            message="'reason' is required.",
            details=[{"field": "reason", "issue": "required, non-empty"}],
        )
    survey.pdf_reupload_required = True
    survey.pdf_reupload_reason = reason.strip()
    survey.pdf_reupload_count = (survey.pdf_reupload_count or 0) + 1
    db.session.commit()
    return survey


def complete_pdf_reupload(survey_id, pdf_url):
    """routes/captain.py:1074-1146"""
    from extensions import db

    survey = lock_survey(survey_id)
    if not survey.pdf_reupload_required:
        raise Conflict(
            message="No PDF re-upload was requested for this survey.",
            code="reupload_not_requested",
        )
    survey.end_survey_pdf = pdf_url
    survey.pdf_reupload_required = False
    survey.pdf_reupload_reason = None
    db.session.commit()
    return survey


# ---------------------------------------------------------------------------
# Review / tasks
# ---------------------------------------------------------------------------


def roadvision_review(survey_id, remark):
    """
    routes/roadvision.py:336-356 - one-shot.

    The portal enforces this only by redirecting; here a second attempt is a 409.
    """
    from extensions import db

    survey = lock_survey(survey_id)
    if survey.roadvision_completed:
        raise Conflict(
            message="This survey has already been reviewed by RoadVision.",
            code="already_reviewed",
        )
    if not (remark or "").strip():
        raise ValidationError(
            message="'remark' is required.",
            details=[{"field": "remark", "issue": "required, non-empty"}],
        )

    survey.roadvision_completed = True
    survey.roadvision_remark = remark.strip()
    survey.roadvision_completed_at = utc_now()
    db.session.commit()
    return survey


def toggle_task(survey_id, task, completed=True, allow_reset=False):
    """
    Mark a team-leader task done - routes/teamleader.py:260-296, 398-414.

    ``task`` is ``task1`` (shown as "Raw Video"), ``task2`` ("Final Report") or
    ``survey_form`` ("Survey Form").
    """
    from extensions import db

    if task not in TASK_FIELDS:
        raise ValidationError(
            message=f"Unknown task '{task}'.",
            details=[
                {
                    "field": "task",
                    "issue": "unknown",
                    "value": task,
                    "allowed": list(TASK_FIELDS),
                }
            ],
        )

    field, at_field, _label = TASK_FIELDS[task]
    survey = lock_survey(survey_id)

    if getattr(survey, field) and completed:
        raise Conflict(
            message=f"Task '{task}' is already complete.",
            code="task_already_complete",
        )
    if not completed and not allow_reset:
        raise Conflict(
            message=(
                f"Task '{task}' cannot be un-completed. "
                "Pass allow_reset=true if you really mean to."
            ),
            code="task_reset_forbidden",
        )

    setattr(survey, field, bool(completed))
    setattr(survey, at_field, utc_now() if completed else None)
    db.session.commit()
    return survey


def set_visibility(survey_id, show_on_dashboard=None, show_in_teamleader_dashboard=None):
    """Control which dashboards a survey appears on."""
    from extensions import db

    survey = lock_survey(survey_id)
    if show_on_dashboard is not None:
        survey.show_on_dashboard = bool(show_on_dashboard)
    if show_in_teamleader_dashboard is not None:
        survey.show_in_teamleader_dashboard = bool(show_in_teamleader_dashboard)
    db.session.commit()
    return survey


def active_survey_for(captain_email):
    """
    The survey that locks a captain out of starting another.

    routes/captain.py:44-55 and friends redirect to /recording whenever one of
    these exists.
    """
    from models.db_models import Survey

    return (
        Survey.query.filter(
            Survey.captain_email == captain_email,
            Survey.status.in_(list(IN_PROGRESS_STATUSES)),
        )
        .order_by(Survey.id.desc())
        .first()
    )

# ==========================================================================
# svc_alerts
# The missed-survey engine and the Monday reset.
#
# In the portal both of these run as side effects of a GET: ``GET /admin`` performs
# the weekly reset (routes/admin.py:61-84) and the missed-survey evaluation
# (:404-544), then commits, and ``GET /regional`` runs a second, slightly different
# copy (routes/regional.py:210-297).
#
# The API must not inherit that.  A machine client polling ``GET /surveys`` every
# 30 seconds would be silently rewriting assignment statuses in production.  So
# here the logic is split:
#
#     compute_alerts()     read-only.  Never writes.  Used by dashboards.
#     run_missed_engine()  the writing version, only reachable through
#                          POST /alerts/evaluate or POST /jobs/tick.
#     run_weekly_reset()   likewise, via POST /assignments/weekly-reset.
#
# The portal's own behaviour is unchanged - its inline copies still run. That means
# the portal and the API can briefly disagree on the missed count if nobody has
# loaded /admin recently, which is the honest trade for not touching routes/.
#
# A note on the two divergent copies
# ----------------------------------
# The portal's two inline implementations no longer agree, so ``variant`` selects
# which one to mirror:
#
#   variant="admin"     routes/admin.py.  One cutoff only (14:00).  With two or
#                       more assignments and none started, marks EVERY row and
#                       raises one alert per unacknowledged row.  Nothing at all
#                       happens after 14:00 - the second cutoff was removed.
#
#   variant="regional"  routes/regional.py (the default here).  Two cutoffs.  At
#                       14:00 marks only the FIRST row; at 16:00, if some but not
#                       all surveys started, marks every row that has not.
#
# admin.py used to carry a loop-variable leak at its 16:00 branch that marked one
# arbitrary row; that whole branch is gone, so there is no longer a bug to
# reproduce.  The variants now differ in substance, not in fidelity - which one
# is "right" is a product question the portal has not settled.
# ==========================================================================

import logging

from core.config import (
    ACTIVE_SURVEY_STATUSES,
    ALERT_CUTOFF_HOUR_PRIMARY,
    ALERT_CUTOFF_HOUR_SECONDARY,
)
from core.config import ist_now, week_start_monday

log = logging.getLogger("viama.api.alerts")


def _job_lock(name="viama_missed_engine"):
    """
    Stop two callers running the engine at once.

    ``pg_try_advisory_xact_lock`` releases at transaction end, so a killed
    serverless function cannot leave the lock held.  Returns True on non-Postgres
    (SQLite has no advisory locks and no concurrency to speak of).
    """
    from sqlalchemy import text

    from extensions import db

    try:
        return bool(
            db.session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": name}
            ).scalar()
        )
    except Exception:
        db.session.rollback()
        return True


def _survey_started(section_no, week_start):
    from models.db_models import Survey

    return (
        Survey.query.filter(
            Survey.section_no == section_no,
            Survey.start_time >= week_start,
            Survey.status.in_(list(ACTIVE_SURVEY_STATUSES)),
        ).first()
        is not None
    )


def evaluate(states=None, now=None, variant="regional"):
    """
    Work out which assignments are missed, WITHOUT writing anything.

    Returns ``{"alerts": [...], "changes": [...], "evaluated_at_ist": ...}``.
    ``changes`` is what *would* be written - the dry-run preview.
    """
    from models.db_models import SurveyAssignment

    now = now or ist_now()
    today = now.strftime("%A")
    current_hour = now.hour
    week_start = week_start_monday(now)

    query = SurveyAssignment.query.filter_by(survey_day=today, survey_enabled=True)
    if states:
        query = query.filter(SurveyAssignment.state.in_(list(states)))
    todays = query.all()

    alerts = []
    changes = []

    if current_hour < ALERT_CUTOFF_HOUR_PRIMARY:
        return {
            "alerts": alerts,
            "changes": changes,
            "evaluated_at_ist": now.isoformat(),
            "day": today,
            "hour_ist": current_hour,
            "assignments_considered": len(todays),
            "note": (
                f"Before {ALERT_CUTOFF_HOUR_PRIMARY}:00 IST nothing is marked missed."
            ),
        }

    by_captain = {}
    for assignment in todays:
        by_captain.setdefault(assignment.captain_email, []).append(assignment)

    for _email, assignments in by_captain.items():
        total = len(assignments)
        started = 0

        for assignment in assignments:
            if _survey_started(assignment.section_no, week_start):
                started += 1
                if assignment.status == "missed":
                    changes.append(
                        {
                            "assignment_id": assignment.id,
                            "from": assignment.status,
                            "to": "started",
                            "clears_acknowledgement": True,
                        }
                    )

        def raise_alert(target, message):
            alerts.append(
                {
                    "assignment_id": target.id,
                    "captain": target.main_person,
                    "message": message,
                    "state": target.state,
                    "stretch": target.stretch_code,
                    "section_no": target.section_no,
                    "reason": target.missed_reason,
                }
            )

        if total == 1:
            if started == 0:
                changes.append(
                    {"assignment_id": assignments[0].id,
                     "from": assignments[0].status, "to": "missed"}
                )
                if not assignments[0].alert_acknowledged:
                    raise_alert(assignments[0], "Survey not started by 2 PM")

        elif total >= 2:
            if started == 0:
                if variant == "admin":
                    # routes/admin.py:546-570 - marks EVERY assignment and
                    # raises one alert per unacknowledged row. This is the branch
                    # that used to contain the loop-variable leak; the portal has
                    # since fixed it, so there is no longer a bug to reproduce.
                    for assignment in assignments:
                        changes.append(
                            {
                                "assignment_id": assignment.id,
                                "from": assignment.status,
                                "to": "missed",
                            }
                        )
                        if not assignment.alert_acknowledged:
                            raise_alert(assignment, "Survey not started by 2 PM")
                else:
                    # routes/regional.py:282-284 - still marks only the first row.
                    changes.append(
                        {"assignment_id": assignments[0].id,
                         "from": assignments[0].status, "to": "missed"}
                    )
                    if not assignments[0].alert_acknowledged:
                        raise_alert(assignments[0], "Survey not started by 2 PM")

            elif (
                variant != "admin"
                and current_hour >= ALERT_CUTOFF_HOUR_SECONDARY
                and started < total
            ):
                # routes/regional.py:286-295 ONLY. routes/admin.py dropped its
                # second cutoff entirely, so under variant="admin" nothing more
                # happens after 14:00 - that is the real divergence now.
                for assignment in assignments:
                    if not _survey_started(assignment.section_no, week_start):
                        changes.append(
                            {
                                "assignment_id": assignment.id,
                                "from": assignment.status,
                                "to": "missed",
                            }
                        )
                if not assignments[0].alert_acknowledged:
                    raise_alert(
                        assignments[0],
                        f"Survey not started by {ALERT_CUTOFF_HOUR_SECONDARY % 12} PM",
                    )

    return {
        "alerts": alerts,
        "changes": changes,
        "evaluated_at_ist": now.isoformat(),
        "day": today,
        "hour_ist": current_hour,
        "assignments_considered": len(todays),
        "variant": variant,
    }


def compute_alerts(states=None, now=None, variant="regional"):
    """Read-only alert list, for dashboards. Guaranteed not to write."""
    return evaluate(states=states, now=now, variant=variant)["alerts"]


def run_missed_engine(states=None, now=None, variant="regional", dry_run=False):
    """Evaluate and persist. Only called from an explicit POST."""
    from extensions import db
    from models.db_models import SurveyAssignment

    result = evaluate(states=states, now=now, variant=variant)

    if dry_run:
        result["committed"] = False
        result["changed_count"] = 0
        return result

    if not _job_lock():
        result.update(
            {"committed": False, "skipped": "another run is already in progress"}
        )
        return result

    changed = 0
    for change in result["changes"]:
        assignment = db.session.get(SurveyAssignment, change["assignment_id"])
        if not assignment or assignment.status == change["to"]:
            continue
        assignment.status = change["to"]
        if change.get("clears_acknowledgement"):
            assignment.alert_acknowledged = False
        changed += 1

    if changed:
        db.session.commit()

    result["committed"] = True
    result["changed_count"] = changed
    return result


def run_weekly_reset(now=None, force=False, dry_run=False):
    """
    The Monday reset - routes/admin.py:61-84.

    Only acts on a Monday unless ``force``.  The portal's version is an
    unfiltered bulk ``Query.update()`` over every assignment row; this one is
    ORM-driven so the change-capture hooks can see it, and it reports how many
    rows it touched.
    """
    from extensions import db
    from models.db_models import SurveyAssignment

    now = now or ist_now()
    today = now.strftime("%A")
    today_date = now.date()

    if today != "Monday" and not force:
        return {
            "ran": False,
            "reason": f"today is {today}; the reset only runs on Monday (use force=true)",
            "reset_count": 0,
        }

    pending = SurveyAssignment.query.filter(
        db.or_(
            SurveyAssignment.last_week_reset.is_(None),
            SurveyAssignment.last_week_reset < today_date,
        )
    ).all()

    if dry_run:
        return {
            "ran": False,
            "dry_run": True,
            "would_reset": len(pending),
            "last_week_reset": today_date.isoformat(),
        }

    if not pending:
        return {"ran": False, "reason": "already reset for this week", "reset_count": 0}

    if not _job_lock("viama_weekly_reset"):
        return {"ran": False, "reason": "another reset is already in progress",
                "reset_count": 0}

    for assignment in pending:
        assignment.status = "assigned"
        assignment.alert_acknowledged = False
        assignment.last_week_reset = today_date

    db.session.commit()

    return {
        "ran": True,
        "reset_count": len(pending),
        "last_week_reset": today_date.isoformat(),
    }


def missed_count(states=None):
    """Current missed count, as the dashboards report it."""
    from models.db_models import SurveyAssignment

    query = SurveyAssignment.query.filter_by(status="missed")
    if states:
        query = query.filter(SurveyAssignment.state.in_(list(states)))
    return query.count()

# ==========================================================================
# svc_media
# File uploads.
#
# Reuses the portal's existing helpers unchanged - google_drive.upload_file_to_drive,
# pdf_utils.optimize_pdf, utils.image_compressor.compress_image - so a file uploaded
# through the API is byte-identical in treatment to one uploaded through the portal.
#
# Every import is deferred into the function body on purpose, so a broken media
# dependency can never stop the API booting.  google_drive.py also resolves its
# credentials lazily now, which means an expired token fails only the upload that
# needs it.  Failures map to 503, not 500 - an expired Google token is an upstream
# problem, not a bug in this request.
# ==========================================================================

import logging
import os

# (merged) now defined in this module: ServiceUnavailable, UnsupportedMediaType, ValidationError

log = logging.getLogger("viama.api.media")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_FILENAME = 120


def _require_file(storage, field):
    if storage is None or not storage.filename:
        raise ValidationError(
            message=f"Upload a file in the '{field}' multipart field.",
            details=[{"field": field, "issue": "required"}],
        )


def _safe_name(name):
    keep = "".join(c for c in (name or "") if c.isalnum() or c in "._- ")
    return (keep.strip() or "upload")[:MAX_FILENAME]


def upload_image(storage, compress=True, folder_id=None):
    """Compress and push an image to Drive; returns the Drive result dict."""
    _require_file(storage, "file")

    if storage.mimetype and storage.mimetype not in ALLOWED_IMAGE_TYPES:
        raise UnsupportedMediaType(
            message=f"'{storage.mimetype}' is not an accepted image type.",
            details=[
                {"field": "file", "issue": "unsupported type",
                 "value": storage.mimetype, "allowed": sorted(ALLOWED_IMAGE_TYPES)}
            ],
        )

    try:
        from config_drive import IMAGE_FOLDER_ID
        from google_drive import upload_file_to_drive
    except Exception as exc:
        raise ServiceUnavailable(
            message="Google Drive is not configured on this deployment.",
            code="media_backend_unavailable",
        ) from exc

    data = storage.read()

    if compress:
        try:
            from utils.image_compressor import compress_image

            storage.stream.seek(0)
            compressed = compress_image(storage)
            if compressed:
                data = compressed.read() if hasattr(compressed, "read") else compressed
        except Exception:
            # Compression is an optimisation; a failure should not lose the upload.
            log.warning("image compression failed; uploading the original", exc_info=True)

    try:
        return upload_file_to_drive(
            file_bytes=data,
            filename=_safe_name(storage.filename),
            folder_id=folder_id or IMAGE_FOLDER_ID,
            mime_type=storage.mimetype or "image/jpeg",
        )
    except Exception as exc:
        log.exception("Drive image upload failed")
        raise ServiceUnavailable(
            message="Could not upload to Google Drive.",
            code="media_backend_unavailable",
        ) from exc


def upload_pdf(storage, optimize=True, filename=None, folder_id=None):
    """Optimise and push a PDF to Drive."""
    _require_file(storage, "file")

    name = (storage.filename or "").lower()
    if storage.mimetype != "application/pdf" and not name.endswith(".pdf"):
        raise UnsupportedMediaType(
            message="Only PDF files are accepted here.",
            details=[{"field": "file", "issue": "must be application/pdf",
                      "value": storage.mimetype}],
        )

    try:
        from config_drive import PDF_FOLDER_ID
        from google_drive import upload_file_to_drive
    except Exception as exc:
        raise ServiceUnavailable(
            message="Google Drive is not configured on this deployment.",
            code="media_backend_unavailable",
        ) from exc

    data = storage.read()

    if optimize:
        try:
            from pdf_utils import optimize_pdf

            data = optimize_pdf(data)
        except Exception:
            log.warning("PDF optimisation failed; uploading the original", exc_info=True)

    try:
        return upload_file_to_drive(
            file_bytes=data,
            filename=_safe_name(filename or storage.filename),
            folder_id=folder_id or PDF_FOLDER_ID,
            mime_type="application/pdf",
        )
    except Exception as exc:
        log.exception("Drive PDF upload failed")
        raise ServiceUnavailable(
            message="Could not upload to Google Drive.",
            code="media_backend_unavailable",
        ) from exc


def upload_survey_pdf(storage, survey):
    """
    Upload an end-of-survey PDF under the portal's naming convention.

    ``{upc_code}_Cycle-{n}_Section-{s}.pdf`` - routes/captain.py:504-508.
    """
    filename = (
        f"{survey.upc_code}_Cycle-{survey.cycle_no}_Section-{survey.section_no}.pdf"
    )
    return upload_pdf(storage, optimize=True, filename=filename)


def upload_pdf_to_supabase(storage):
    """
    The alternative storage path.

    routes/captain.py:1106-1131 uses Supabase Storage for PDF *re*-uploads while
    the initial upload goes to Drive, so ``end_survey_pdf`` can hold a URL from
    either origin depending on history.
    """
    import uuid

    _require_file(storage, "file")

    try:
        from supabase_client import supabase

        from core.config import SUPABASE_PDF_BUCKET
    except Exception as exc:
        raise ServiceUnavailable(
            message="Supabase storage is not configured on this deployment.",
            code="media_backend_unavailable",
        ) from exc

    object_name = f"{uuid.uuid4().hex}.pdf"
    data = storage.read()

    try:
        supabase.storage.from_(SUPABASE_PDF_BUCKET).upload(
            object_name, data, {"content-type": "application/pdf"}
        )
        url = supabase.storage.from_(SUPABASE_PDF_BUCKET).get_public_url(object_name)
    except Exception as exc:
        log.exception("Supabase upload failed")
        raise ServiceUnavailable(
            message="Could not upload to Supabase storage.",
            code="media_backend_unavailable",
        ) from exc

    return {"url": url, "object_name": object_name, "bucket": SUPABASE_PDF_BUCKET}


def media_backends_status():
    """Which storage backends look usable - surfaced by GET /media/status."""
    # Importing google_drive proves nothing now that credentials load lazily, so
    # actually build the client. This is the check an operator wants: it fails
    # here for exactly the reasons an upload would.
    from core.config import SUPABASE_PDF_BUCKET

    drive_ok, drive_error, drive_source = True, None, None
    drive_token_path = None
    try:
        import google_drive

        drive_token_path = google_drive.DRIVE_TOKEN_PATH
        google_drive.get_drive()
        drive_source = (
            "GOOGLE_SA_JSON" if os.getenv("GOOGLE_SA_JSON") else "token_drive.pickle"
        )
    except Exception as exc:
        drive_ok, drive_error = False, f"{type(exc).__name__}: {exc}"[:200]

    # Gmail is a separate credential with a separate expiry: the admin
    # draft-builder can be broken while Drive uploads are perfectly fine.
    gmail_ok, gmail_error, gmail_token_path = True, None, None
    try:
        import google_drive

        gmail_token_path = google_drive.GMAIL_TOKEN_PATH
        google_drive.get_gmail()
    except Exception as exc:
        gmail_ok, gmail_error = False, f"{type(exc).__name__}: {exc}"[:200]

    supabase_ok, supabase_error = True, None
    try:
        import supabase_client  # noqa: F401
    except Exception as exc:
        supabase_ok, supabase_error = False, f"{type(exc).__name__}: {exc}"[:200]

    return {
        "google_drive": {
            "available": drive_ok,
            "error": drive_error,
            "credential_source": drive_source,
            "note": (
                "Credentials resolve on first upload. Set GOOGLE_SA_JSON to use a "
                "service account instead of token_drive.pickle; the OAuth token "
                "expiring makes every media endpoint return 503 until refreshed."
            ),
        },
        "gmail": {
            "available": gmail_ok,
            "error": gmail_error,
            "note": (
                "Separate credential from Drive. Only the admin Gmail-draft "
                "builder needs it; Drive uploads are unaffected by its expiry."
            ),
        },
        "supabase_storage": {
            "available": supabase_ok,
            "error": supabase_error,
            "bucket": SUPABASE_PDF_BUCKET,
        },
        "token_pickle_present": {
            "drive": bool(drive_token_path) and os.path.exists(drive_token_path),
            "gmail": bool(gmail_token_path) and os.path.exists(gmail_token_path),
        },
    }

# ==========================================================================
# hooks
# Change capture.
#
# This is what makes push work without editing a single portal route.  Every write
# in the app - whether it came through /api/v1 or through a captain tapping a
# button in the HTML UI - goes through ``db.session``, so listening at the session
# level catches all of it.
#
# Two listeners:
#
# ``before_flush``  builds the diff.  It has to happen here: once the transaction
#                   commits, the ORM attribute history is gone and no query is
#                   safe to run inside ``after_commit``.
#
# ``after_commit``  translates the raw diffs into semantic events and writes them
#                   to ``webhook_outbox`` + ``change_log``.  Only after the
#                   business transaction has actually landed, so an event can never
#                   describe a change that got rolled back.
#
# Bulk ``Query.update()`` bypasses the ORM unit of work entirely, so nothing here
# sees it.  Both portal sites that did this are now covered: acknowledge-alert
# iterates the handful of rows it touches, and the Monday reset keeps its bulk
# statement for speed but calls ``record_bulk_change`` afterwards to announce
# itself.  Any NEW bulk update must do one or the other, or the consuming site
# silently misses it.
# ==========================================================================

import logging
import os

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm.attributes import get_history

log = logging.getLogger("viama.api.hooks")

_INSTALLED = False

#: model class name -> (entity slug, event prefix)
TRACKED = {
    "Survey": ("survey", "survey"),
    "SurveyAssignment": ("assignment", "assignment"),
    "User": ("user", "user"),
    "SurveySchedule": ("schedule", "schedule"),
    "Equipment": ("equipment", "equipment"),
    "RegionalManagerState": ("manager_state", "manager_state"),
}

#: Never put these in an event payload.
REDACTED_FIELDS = {"password_hash"}


def bulk_threshold():
    try:
        return int(os.getenv("WEBHOOK_BULK_THRESHOLD", "25"))
    except ValueError:
        return 25


def capture_enabled():
    return os.getenv("WEBHOOK_CAPTURE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _column_names(obj):
    return [column.key for column in sa_inspect(obj.__class__).mapper.column_attrs]


def _snapshot(obj):
    """Plain dict of the object's columns, JSON-safe, with secrets removed."""
    from core.engine import jsonify_value

    data = {}
    for name in _column_names(obj):
        if name in REDACTED_FIELDS:
            continue
        data[name] = jsonify_value(getattr(obj, name, None))
    return data


def _diff(obj):
    """``{field: {"from": old, "to": new}}`` for the attributes actually changed."""
    from core.engine import jsonify_value

    changed = {}
    for name in _column_names(obj):
        if name in REDACTED_FIELDS:
            continue
        history = get_history(obj, name)
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        if old == new:
            continue
        changed[name] = {"from": jsonify_value(old), "to": jsonify_value(new)}
    return changed


def _actor():
    """
    Who caused this change, without editing any route.

    Reads the API client if one is authenticated, otherwise the portal session.
    """
    try:
        from flask import has_request_context, session

        from core.engine import current_client

        if not has_request_context():
            return {"type": "system"}, "system"

        client = current_client()
        if client:
            return (
                {"type": "api_token", "sub": client.subject, "jti": client.jti},
                "api",
            )

        user_id = session.get("user_id")
        if user_id:
            return (
                {"type": "session", "user_id": user_id, "role": session.get("role")},
                "portal",
            )
        return {"type": "anonymous"}, "portal"
    except Exception:
        return {"type": "system"}, "system"


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------


def _on_before_flush(session, flush_context, instances):
    if not capture_enabled():
        return

    try:
        pending = session.info.setdefault("_viama_changes", [])

        for obj in session.new:
            entry = _classify(obj, "insert")
            if entry:
                pending.append(entry)

        for obj in session.dirty:
            if not session.is_modified(obj, include_collections=False):
                continue
            entry = _classify(obj, "update")
            if entry and entry["changed"]:
                pending.append(entry)

        for obj in session.deleted:
            entry = _classify(obj, "delete")
            if entry:
                pending.append(entry)
    except Exception:
        # Capture must never break a write.
        log.exception("change capture failed during before_flush")


def _classify(obj, op):
    meta = TRACKED.get(type(obj).__name__)
    if not meta:
        return None
    entity, _prefix = meta
    return {
        "entity": entity,
        "model": type(obj).__name__,
        "op": op,
        "obj": obj,
        "changed": _diff(obj) if op == "update" else {},
        "snapshot": _snapshot(obj),
        # Set by crud.delete_resource. A soft delete reaches this listener as an
        # ordinary UPDATE, so the flag is the only way to tell the two apart.
        "soft_deleted": bool(getattr(obj, "_viama_soft_deleted", False)),
    }


def _on_after_flush(session, flush_context):
    """
    Backfill primary keys.

    ``before_flush`` necessarily runs before the INSERT, so a newly created row
    has ``id = None`` at diff time.  By ``after_flush`` the database has assigned
    it, and we are still inside the transaction where reading it is free - so
    every ``*.created`` event carries a real id instead of null.
    """
    pending = session.info.get("_viama_changes")
    if not pending:
        return
    for entry in pending:
        obj = entry.get("obj")
        if obj is None:
            continue
        try:
            identifier = getattr(obj, "id", None)
            if identifier is not None:
                entry["snapshot"]["id"] = identifier
        except Exception:
            continue


def _on_after_commit(session):
    if not capture_enabled():
        return

    pending = session.info.pop("_viama_changes", None)
    if not pending:
        return

    try:
        from core.engine import record_changes

        record_changes(pending, *_actor())
    except Exception:
        log.exception("failed to record %d change(s) to the outbox", len(pending))


def _on_rollback(session):
    session.info.pop("_viama_changes", None)


def install_hooks(app):
    """Attach the session listeners. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return

    from extensions import db

    event.listen(db.session, "before_flush", _on_before_flush)
    event.listen(db.session, "after_flush", _on_after_flush)
    event.listen(db.session, "after_commit", _on_after_commit)
    event.listen(db.session, "after_rollback", _on_rollback)

    _INSTALLED = True
    log.info("viama: change-capture hooks installed")

# ==========================================================================
# events
# Turn raw ORM diffs into meaningful events, then persist them.
#
# ``survey.updated`` is nearly useless to a consumer; ``survey.completed`` and
# ``survey.roadvision_reviewed`` are not.  This module does that translation, then
# writes each event to two places:
#
#   webhook_outbox  the delivery queue (push)
#   change_log      the ordered feed behind /api/v1/sync/changes (pull catch-up)
#
# Storm control matters here.  ``GET /admin`` on a Monday bulk-updates every
# assignment row, and any dashboard load can flip several at once, so a large
# single-model commit collapses to one ``*.bulk_changed`` event instead of N.
# ==========================================================================

import logging
import os
import uuid

from core.config import (
    SURVEY_COMPLETED,
    SURVEY_GROUNDWORK_COMPLETED,
    SURVEY_VIDEO_PENDING,
    TASK_FIELDS,
)
from core.config import ist_now_aware, iso, utc_now

log = logging.getLogger("viama.api.events")

#: Full catalog, also served by GET /api/v1/webhooks/events.
EVENT_CATALOG = {
    "survey.created": "A survey was started (checklist submitted).",
    "survey.updated": "One or more survey fields changed.",
    "survey.status_changed": "The survey moved to a different status.",
    "survey.groundwork_submitted": "Status moved to video_pending.",
    "survey.completed": "Status moved to completed.",
    "survey.cancelled": "The survey was cancelled via the API.",
    "survey.deleted": "The survey was deleted (soft or hard).",
    "survey.video_counts_submitted": "The 8 defect counts were recorded.",
    "survey.video_count_checked": "The automated bucket-count check ran.",
    "survey.resurvey_requested": "A captain asked for a re-survey.",
    "survey.resurvey_approved": "An admin approved a re-survey.",
    "survey.pdf_reupload_requested": "An admin asked for the PDF again.",
    "survey.pdf_reuploaded": "A captain supplied a replacement PDF.",
    "survey.roadvision_reviewed": "RoadVision acknowledged the survey.",
    "survey.task_toggled": "A team-leader task was marked done.",
    "survey.media_updated": "A photo or PDF URL changed.",
    "assignment.created": "A new assignment row.",
    "assignment.updated": "Assignment fields changed.",
    "assignment.missed": "An assignment was marked missed.",
    "assignment.alert_acknowledged": "A missed alert was acknowledged.",
    "assignment.bulk_changed": "Many assignments changed at once (e.g. Monday reset).",
    "assignment.deleted": "An assignment was deleted.",
    "user.created": "A user was created.",
    "user.updated": "User fields changed.",
    "user.deleted": "A user was deleted.",
    "schedule.created": "A legacy survey_schedule row was created.",
    "schedule.updated": "A legacy survey_schedule row changed.",
    "schedule.deleted": "A legacy survey_schedule row was deleted.",
    "equipment.created": "Equipment was created.",
    "equipment.updated": "Equipment changed.",
    "equipment.deleted": "Equipment was deleted.",
    "manager_state.created": "A regional manager gained a state.",
    "manager_state.updated": "A regional-manager/state mapping changed.",
    "manager_state.deleted": "A regional manager lost a state.",
}


def enabled_events():
    """
    ``WEBHOOK_ENABLED_EVENTS`` allowlist.

    Ships excluding ``assignment.updated``: the portal rewrites assignment rows
    on ordinary dashboard loads, so that event is almost entirely noise.  Set the
    env var to ``*`` to receive everything.
    """
    raw = os.getenv("WEBHOOK_ENABLED_EVENTS", "").strip()
    if not raw:
        return None  # -> default policy below
    if raw == "*":
        return "*"
    return {item.strip() for item in raw.split(",") if item.strip()}


DEFAULT_MUTED = {"assignment.updated"}


def _is_enabled(event):
    allow = enabled_events()
    if allow == "*":
        return True
    if allow is None:
        return event not in DEFAULT_MUTED
    return event in allow


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _survey_events(entry):
    """Semantic event names for one Survey change."""
    changed = entry["changed"]
    snapshot = entry["snapshot"]

    if entry["op"] == "insert":
        return ["survey.created"]
    if entry["op"] == "delete" or entry.get("soft_deleted"):
        return ["survey.deleted"]

    events = []

    if "status" in changed:
        events.append("survey.status_changed")
        target = changed["status"]["to"]
        if target == SURVEY_VIDEO_PENDING:
            events.append("survey.groundwork_submitted")
        elif target == SURVEY_COMPLETED:
            events.append("survey.completed")
        elif target == "cancelled":
            events.append("survey.cancelled")
        elif target == SURVEY_GROUNDWORK_COMPLETED:
            events.append("survey.groundwork_submitted")

    if changed.get("video_uploaded", {}).get("to") is True:
        events.append("survey.video_counts_submitted")

    if "video_count_matched" in changed or "video_count_checked_at" in changed:
        events.append("survey.video_count_checked")

    if changed.get("resurvey_requested", {}).get("to") is True:
        events.append("survey.resurvey_requested")
    if changed.get("resurvey_approved", {}).get("to") is True:
        events.append("survey.resurvey_approved")

    if changed.get("pdf_reupload_required", {}).get("to") is True:
        events.append("survey.pdf_reupload_requested")
    elif changed.get("pdf_reupload_required", {}).get("to") is False and (
        "end_survey_pdf" in changed
    ):
        events.append("survey.pdf_reuploaded")

    if changed.get("roadvision_completed", {}).get("to") is True:
        events.append("survey.roadvision_reviewed")

    for key, (field, _at, _label) in TASK_FIELDS.items():
        if changed.get(field, {}).get("to") is True:
            events.append("survey.task_toggled")
            entry.setdefault("context", {})["task"] = key

    if any(
        field in changed
        for field in (
            "dashcam_photo",
            "end_survey_pdf",
            "end_survey_photo",
            # RoadVision spreadsheet uploads (routes/roadvision.py). Without
            # these the consuming site never learns a defect report landed.
            "defect_report_file",
            "raw_video_excel_file",
        )
    ):
        events.append("survey.media_updated")

    if not events:
        events.append("survey.updated")

    # De-duplicate, preserving order.
    seen, ordered = set(), []
    for event in events:
        if event not in seen:
            seen.add(event)
            ordered.append(event)
    _ = snapshot
    return ordered


def _assignment_events(entry):
    if entry["op"] == "insert":
        return ["assignment.created"]
    if entry["op"] == "delete" or entry.get("soft_deleted"):
        return ["assignment.deleted"]

    changed = entry["changed"]
    events = []
    if changed.get("status", {}).get("to") == "missed":
        events.append("assignment.missed")
    if changed.get("alert_acknowledged", {}).get("to") is True:
        events.append("assignment.alert_acknowledged")
    if not events:
        events.append("assignment.updated")
    return events


_SIMPLE = {
    "user": "user",
    "schedule": "schedule",
    "equipment": "equipment",
    "manager_state": "manager_state",
}


def events_for(entry):
    entity = entry["entity"]
    if entity == "survey":
        return _survey_events(entry)
    if entity == "assignment":
        return _assignment_events(entry)

    prefix = _SIMPLE.get(entity, entity)
    if entry.get("soft_deleted"):
        return [f"{prefix}.deleted"]
    return [f"{prefix}.{'created' if entry['op'] == 'insert' else entry['op'] + 'd'}"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def build_payload(event, entry, actor, source, context=None):
    now = utc_now()
    return {
        "id": uuid.uuid4().hex,
        "event": event,
        "occurred_at_utc": iso(now.replace(tzinfo=None)) + "Z",
        "occurred_at_ist": iso(ist_now_aware()),
        "api_version": "v1",
        "source": source,
        "actor": actor,
        "data": {
            "object": entry["entity"],
            "id": entry["snapshot"].get("id"),
            "op": entry["op"],
            "changed": entry["changed"] or None,
            "snapshot": entry["snapshot"],
            "context": context or entry.get("context") or None,
        },
    }


def record_changes(entries, actor, source):
    """
    Write the outbox + change-log rows for a committed batch.

    Uses a SEPARATE session, not ``db.session``.  This runs from an
    ``after_commit`` listener, where the originating session is already in the
    'committed' state and can emit no further SQL - and a fresh session also
    means a failure here can never roll back real business data.  It does not
    re-trigger the listener, which is registered on the scoped session only.
    """
    from sqlalchemy.orm import Session

    from extensions import db

    from core.models import ChangeLog, WebhookOutbox

    if not entries:
        return 0

    # Collapse a large single-entity batch into one event.
    by_entity = {}
    for entry in entries:
        by_entity.setdefault(entry["entity"], []).append(entry)

    from core.engine import bulk_threshold

    threshold = bulk_threshold()
    rows = 0
    session = Session(bind=db.engine)

    try:
        for entity, group in by_entity.items():
            if len(group) > threshold:
                event = f"{entity}.bulk_changed"
                payload = {
                    "id": uuid.uuid4().hex,
                    "event": event,
                    "occurred_at_utc": iso(utc_now().replace(tzinfo=None)) + "Z",
                    "occurred_at_ist": iso(ist_now_aware()),
                    "api_version": "v1",
                    "source": source,
                    "actor": actor,
                    "data": {
                        "object": entity,
                        "count": len(group),
                        "ids": [e["snapshot"].get("id") for e in group][:500],
                        "reason": "bulk update collapsed by WEBHOOK_BULK_THRESHOLD",
                    },
                }
                session.add(
                    ChangeLog(
                        entity=entity,
                        entity_id=None,
                        event=event,
                        op="update",
                        data=payload["data"],
                        actor=actor.get("sub") or str(actor.get("user_id") or ""),
                        source=source,
                    )
                )
                if _is_enabled(event):
                    session.add(
                        WebhookOutbox(
                            event=event,
                            entity=entity,
                            entity_id=None,
                            payload=payload,
                            status="pending",
                        )
                    )
                rows += 1
                continue

            for entry in group:
                for event in events_for(entry):
                    payload = build_payload(event, entry, actor, source)
                    session.add(
                        ChangeLog(
                            entity=entity,
                            entity_id=entry["snapshot"].get("id"),
                            event=event,
                            op=entry["op"],
                            data=payload["data"]["snapshot"],
                            previous=entry["changed"] or None,
                            actor=actor.get("sub") or str(actor.get("user_id") or ""),
                            source=source,
                        )
                    )
                    if _is_enabled(event):
                        session.add(
                            WebhookOutbox(
                                event=event,
                                entity=entity,
                                entity_id=entry["snapshot"].get("id"),
                                payload=payload,
                                status="pending",
                            )
                        )
                    rows += 1

        session.commit()
    except Exception:
        session.rollback()
        log.exception("failed to persist change events")
        return 0
    finally:
        session.close()

    return rows


def record_bulk_change(entity, ids=None, count=None, reason=None):
    """
    Emit a ``<entity>.bulk_changed`` event for a write the ORM never saw.

    ``Query.update()`` goes straight to SQL, so the session listeners in this
    module are blind to it and the consuming site would never learn the rows had
    moved.  The two portal sites that do this - the Monday reset
    (routes/admin.py) and acknowledge-alert - call this afterwards so the feed
    stays honest about what changed.

    Best-effort by design: this must never be able to fail the write it is
    describing, so every error is swallowed and logged.  Returns True if the
    event was persisted.
    """
    from sqlalchemy.orm import Session

    from extensions import db

    from core.models import ChangeLog, WebhookOutbox

    if not capture_enabled():
        return False

    try:
        actor, source = _actor()
        event = f"{entity}.bulk_changed"

        payload = {
            "id": uuid.uuid4().hex,
            "event": event,
            "occurred_at_utc": iso(utc_now().replace(tzinfo=None)) + "Z",
            "occurred_at_ist": iso(ist_now_aware()),
            "api_version": "v1",
            "source": source,
            "actor": actor,
            "data": {
                "object": entity,
                "count": count if count is not None else (len(ids) if ids else None),
                "ids": list(ids)[:500] if ids else None,
                "reason": reason or "bulk SQL update outside the ORM",
            },
        }

        session = Session(bind=db.engine)
        try:
            session.add(
                ChangeLog(
                    entity=entity,
                    entity_id=None,
                    event=event,
                    op="update",
                    data=payload["data"],
                    actor=actor.get("sub") or str(actor.get("user_id") or ""),
                    source=source,
                )
            )
            if _is_enabled(event):
                session.add(
                    WebhookOutbox(
                        event=event,
                        entity=entity,
                        entity_id=None,
                        payload=payload,
                        status="pending",
                    )
                )
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    except Exception:
        log.exception("failed to record bulk change for %s", entity)
        return False

# ==========================================================================
# audit
# Access and authentication auditing.
#
# Records who logged in, from where, on what, and every request they then made -
# without editing routes/auth.py or any other portal file.  It works by wrapping
# the request cycle at app level and watching what happens to the session.
#
# How login detection works
# -------------------------
# routes/auth.py sets ``session["user_id"]`` on success and, on failure, returns the
# bare string ``"Invalid Credentials"`` with HTTP 200 (routes/auth.py:73). So:
#
#   * session had no user_id before the request, and has one after  -> login_success
#   * a POST to the login route that did not set user_id            -> login_failed
#   * session had a user_id before and none after                   -> logout
#
# That covers the real flows without touching the auth code.
#
# Client identity
# ---------------
# ``request.remote_addr`` is the proxy on Vercel, so the real client comes from
# ``X-Forwarded-For`` (leftmost entry).  Vercel also injects geo headers at the
# edge, which are captured when present.
#
# Privacy
# -------
# IP addresses are personal data in most jurisdictions.  ``AUDIT_RETENTION_DAYS``
# (default 365) bounds how long rows live; the purge runs from
# ``POST /api/v1/jobs/tick``.  Set ``AUDIT_LOG_REQUESTS=off`` to keep login events
# only, or ``AUDIT_ANONYMISE_IP=1`` to store a masked address.
# ==========================================================================

import hashlib
import logging
import os
import re
import time
import uuid

from flask import g, has_request_context, request, session

log = logging.getLogger("viama.api.audit")

#: Never worth a row.
SKIP_PREFIXES = ("/static/", "/favicon.ico")

#: The portal's login route (routes/auth.py:20 registers it at "/").
LOGIN_PATHS = {"/", "/login"}
LOGOUT_PATHS = {"/logout"}

#: routes/auth.py returns this literal string on a bad password.
INVALID_CREDENTIALS_MARKER = b"Invalid Credentials"


def _env(name, default):
    return os.getenv(name, default).strip().lower()


def audit_enabled():
    return _env("AUDIT_ENABLED", "1") not in ("0", "false", "no")


def request_log_mode():
    """``all`` | ``api`` | ``mutations`` | ``off``"""
    mode = _env("AUDIT_LOG_REQUESTS", "all")
    return mode if mode in ("all", "api", "mutations", "off") else "all"


def anonymise_ip():
    return _env("AUDIT_ANONYMISE_IP", "0") in ("1", "true", "yes")


def retention_days():
    try:
        return int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
    except ValueError:
        return 365


# ---------------------------------------------------------------------------
# Client fingerprint
# ---------------------------------------------------------------------------


def client_ip():
    """
    Real client IP.

    Behind Vercel (and any proxy) ``remote_addr`` is the edge node, so prefer the
    leftmost X-Forwarded-For entry, which is the original client.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return _maybe_mask(candidate)
    for header in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
        value = request.headers.get(header)
        if value:
            return _maybe_mask(value.strip())
    return _maybe_mask(request.remote_addr or "")


def _maybe_mask(ip):
    if not ip or not anonymise_ip():
        return ip
    if ":" in ip:  # IPv6 -> keep the /48 prefix
        return ":".join(ip.split(":")[:3]) + "::"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["0"])
    return ip


_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
    ("Firefox/", "Firefox"),
    ("MSIE", "Internet Explorer"),
    ("Trident/", "Internet Explorer"),
)

_PLATFORMS = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Windows NT", "Windows"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)

_BOT_PATTERN = re.compile(r"bot|crawl|spider|slurp|curl|wget|python-requests|httpx", re.I)


def parse_user_agent(raw):
    """Cheap UA classification - no external dependency for this."""
    if not raw:
        return {"device_type": None, "browser": None, "platform": None}

    if _BOT_PATTERN.search(raw):
        return {"device_type": "bot", "browser": None, "platform": None}

    browser = next((name for token, name in _BROWSERS if token in raw), None)
    platform = next((name for token, name in _PLATFORMS if token in raw), None)

    if "iPad" in raw or "Tablet" in raw:
        device = "tablet"
    elif "Mobi" in raw or "Android" in raw or "iPhone" in raw:
        device = "mobile"
    else:
        device = "desktop"

    return {"device_type": device, "browser": browser, "platform": platform}


def geo():
    """Edge-provided geo, when the platform supplies it."""
    return {
        "country": request.headers.get("X-Vercel-IP-Country")
        or request.headers.get("CF-IPCountry"),
        "region": request.headers.get("X-Vercel-IP-Country-Region"),
        "city": _decode(request.headers.get("X-Vercel-IP-City")),
        "timezone": request.headers.get("X-Vercel-IP-Timezone"),
    }


def _decode(value):
    if not value:
        return None
    try:
        from urllib.parse import unquote

        return unquote(value)
    except Exception:
        return value


#: Our own key inside the portal's session cookie.
SESSION_ID_KEY = "_viama_sid"


def session_id():
    """
    Stable per-session identifier, so a login can be joined to what followed.

    Stored as an extra key in the Flask session rather than derived from the
    cookie.  Hashing the cookie looks tempting but is wrong here: Flask re-signs
    and re-issues the cookie every time the session is modified, and the captain
    flow writes to the session constantly (survey_id, assignment_id, survey_day
    ...), so a cookie hash would change mid-session and split one visit into many.

    Adding a key is safe: the portal only ever reads keys it set itself, so an
    extra one is inert.
    """
    return session.get(SESSION_ID_KEY)


def ensure_session_id():
    """Assign a session id if the session doesn't have one yet."""
    existing = session.get(SESSION_ID_KEY)
    if existing:
        return existing
    new_id = uuid.uuid4().hex
    session[SESSION_ID_KEY] = new_id
    return new_id


def client_context():
    ua = request.headers.get("User-Agent", "")
    parsed = parse_user_agent(ua)
    location = geo()
    return {
        "ip": client_ip(),
        "forwarded_for": (request.headers.get("X-Forwarded-For") or "")[:255] or None,
        "user_agent": ua or None,
        "device_type": parsed["device_type"],
        "browser": parsed["browser"],
        "platform": parsed["platform"],
        "country": location["country"],
        "region": location["region"],
        "city": location["city"],
        "timezone": location["timezone"],
        "host": request.host,
        "protocol": request.scheme,
        "referer": request.headers.get("Referer"),
        "session_id": session_id(),
        "request_id": getattr(g, "request_id", None),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _write(rows):
    """
    Persist audit rows on an independent session.

    Separate from ``db.session`` so an audit failure can never roll back the
    user's actual work, and so it still works inside a request whose own
    transaction already committed.
    """
    if not rows:
        return
    from sqlalchemy.orm import Session

    from extensions import db

    audit_session = Session(bind=db.engine)
    try:
        audit_session.add_all(rows)
        audit_session.commit()
    except Exception:
        audit_session.rollback()
        log.exception("failed to write %d audit row(s)", len(rows))
    finally:
        audit_session.close()


def record_login_event(event_type, user=None, **overrides):
    """
    Write one authentication event.

    Also callable directly, e.g. from the API's own token-auth path.
    """
    from core.models import LoginEvent

    context = client_context() if has_request_context() else {}
    payload = {
        "event_type": event_type,
        "method": request.method if has_request_context() else None,
        "path": request.path if has_request_context() else None,
        **context,
    }
    if user is not None:
        payload.update(
            {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "name": user.name,
                "role": user.role,
            }
        )
    payload.update(overrides)

    valid = {column.key for column in LoginEvent.__table__.columns}
    _write([LoginEvent(**{k: v for k, v in payload.items() if k in valid})])


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _should_skip():
    path = request.path
    return any(path.startswith(prefix) for prefix in SKIP_PREFIXES)


def _should_log_request(response):
    mode = request_log_mode()
    if mode == "off":
        return False
    if mode == "api":
        return request.path.startswith("/api/")
    if mode == "mutations":
        return request.method not in ("GET", "HEAD", "OPTIONS") or (
            response is not None and response.status_code >= 400
        )
    return True


def _before():
    if not audit_enabled() or _should_skip():
        g.audit_skip = True
        return
    g.audit_skip = False
    g.audit_started = time.perf_counter()
    # Snapshot the session so the after-hook can tell what the request changed.
    # /logout calls session.clear(), so anything needed afterwards must be read
    # here, before the view runs.
    g.audit_user_before = session.get("user_id")
    g.audit_role_before = session.get("role")
    g.audit_session_before = session.get(SESSION_ID_KEY)
    if not getattr(g, "request_id", None):
        g.request_id = uuid.uuid4().hex


def _after(response):
    if getattr(g, "audit_skip", True) or not audit_enabled():
        return response

    try:
        _capture_auth_transition(response)
    except Exception:
        log.exception("login audit failed")

    try:
        if _should_log_request(response):
            _capture_request(response)
    except Exception:
        log.exception("request audit failed")

    return response


def _capture_auth_transition(response):
    from models.db_models import User

    before = getattr(g, "audit_user_before", None)
    after = session.get("user_id")

    if before is None and after is not None:
        user = User.query.get(after)
        # Mint the session id now, on the response that creates the session, so
        # this login event and every later request share the same value.
        record_login_event("login_success", user=user, session_id=ensure_session_id())
        _emit_auth_event("auth.login_success", user)
        return

    if before is not None and after is None and request.path in LOGOUT_PATHS:
        # session.clear() has already run, so the id must come from the snapshot
        # taken before the request.
        record_login_event(
            "logout",
            user=User.query.get(before),
            user_id=before,
            role=getattr(g, "audit_role_before", None),
            session_id=getattr(g, "audit_session_before", None),
        )
        _emit_auth_event("auth.logout", User.query.get(before))
        return

    # A failed login: the portal answers 200 with a plain string, not a 401.
    if (
        request.method == "POST"
        and request.path in LOGIN_PATHS
        and after is None
    ):
        identifier = (
            request.form.get("email")
            or request.form.get("username")
            or request.form.get("login")
        )
        body = b""
        try:
            if not response.direct_passthrough:
                body = response.get_data()[:200]
        except Exception:
            body = b""
        reason = (
            "invalid_credentials"
            if INVALID_CREDENTIALS_MARKER in body
            else "login_not_completed"
        )
        record_login_event(
            "login_failed", login_identifier=identifier, failure_reason=reason
        )
        _emit_auth_event("auth.login_failed", None, identifier=identifier, reason=reason)


def _emit_auth_event(event, user, identifier=None, reason=None):
    """
    Push authentication events to the webhook outbox too.

    Login activity is exactly the kind of thing the other site wants in real
    time, and it does not flow through the ORM change hooks because no tracked
    model is written.
    """
    try:
        from sqlalchemy.orm import Session

        from extensions import db

        from core.models import WebhookOutbox
        from core.engine import _is_enabled
        from core.config import ist_now_aware, iso, utc_now

        if not _is_enabled(event):
            return

        context = client_context()
        payload = {
            "id": uuid.uuid4().hex,
            "event": event,
            "occurred_at_utc": iso(utc_now().replace(tzinfo=None)) + "Z",
            "occurred_at_ist": iso(ist_now_aware()),
            "api_version": "v1",
            "source": "portal",
            "actor": {"type": "session", "user_id": getattr(user, "id", None)},
            "data": {
                "object": "auth",
                "id": getattr(user, "id", None),
                "user": (
                    {
                        "id": user.id,
                        "name": user.name,
                        "email": user.email,
                        "role": user.role,
                    }
                    if user
                    else None
                ),
                "login_identifier": identifier,
                "failure_reason": reason,
                "client": context,
            },
        }

        outbox_session = Session(bind=db.engine)
        try:
            outbox_session.add(
                WebhookOutbox(
                    event=event,
                    entity="auth",
                    entity_id=getattr(user, "id", None),
                    payload=payload,
                    status="pending",
                )
            )
            outbox_session.commit()
        finally:
            outbox_session.close()
    except Exception:
        log.exception("failed to queue %s webhook", event)


def _capture_request(response):
    from core.models import RequestLog
    from core.engine import current_client

    started = getattr(g, "audit_started", None)
    duration_ms = int((time.perf_counter() - started) * 1000) if started else None

    client = current_client()
    context = client_context()

    if client:
        channel = "api"
    elif session.get("user_id"):
        channel = "portal"
    else:
        channel = "anonymous"

    email = None
    if session.get("user_id"):
        try:
            from models.db_models import User

            user = User.query.get(session["user_id"])
            email = user.email if user else None
        except Exception:
            email = None

    error_code = None
    if response.status_code >= 400 and request.path.startswith("/api/"):
        try:
            if not response.direct_passthrough:
                body = response.get_json(silent=True) or {}
                error_code = (body.get("error") or {}).get("code")
        except Exception:
            error_code = None

    _write(
        [
            RequestLog(
                method=request.method,
                path=request.path[:255],
                query_string=(request.query_string or b"").decode("utf-8", "replace")[:2000]
                or None,
                endpoint=request.endpoint,
                status_code=response.status_code,
                duration_ms=duration_ms,
                channel=channel,
                user_id=session.get("user_id"),
                email=email,
                role=session.get("role"),
                token_sub=client.subject if client else None,
                session_id=context["session_id"],
                ip=context["ip"],
                forwarded_for=context["forwarded_for"],
                user_agent=context["user_agent"],
                device_type=context["device_type"],
                country=context["country"],
                city=context["city"],
                referer=context["referer"],
                content_length=request.content_length,
                response_bytes=response.calculate_content_length(),
                request_id=context["request_id"],
                error_code=error_code,
            )
        ]
    )


def install_audit(app):
    """
    Attach the audit hooks. Idempotent per app.

    The guard is stored on the app, NOT in a module global: app.py calls
    create_app() at import time and callers may call it again, so a module-level
    flag would install hooks on the first app and silently leave every later one
    unaudited.
    """
    if app.extensions.get("viama_audit_installed"):
        return

    app.before_request(_before)
    app.after_request(_after)

    app.extensions["viama_audit_installed"] = True
    log.info("viama: audit hooks installed (mode=%s)", request_log_mode())


def purge_old(days=None):
    """Delete audit rows past the retention window. Called by /jobs/tick."""
    from datetime import datetime, timedelta

    from sqlalchemy.orm import Session

    from extensions import db

    from core.models import LoginEvent, RequestLog

    days = days or retention_days()
    cutoff = datetime.utcnow() - timedelta(days=days)

    purge_session = Session(bind=db.engine)
    try:
        logins = (
            purge_session.query(LoginEvent)
            .filter(LoginEvent.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        requests_removed = (
            purge_session.query(RequestLog)
            .filter(RequestLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        purge_session.commit()
        return {
            "retention_days": days,
            "login_events_removed": int(logins or 0),
            "request_logs_removed": int(requests_removed or 0),
        }
    except Exception:
        purge_session.rollback()
        log.exception("audit purge failed")
        return {"error": "purge_failed"}
    finally:
        purge_session.close()

# ==========================================================================
# webhooks
# Webhook delivery.
#
# The outbox row is written inside the business transaction (core/hooks.py), so an
# event is never lost.  This module is only concerned with getting it out.
#
# Why delivery is not inline
# --------------------------
# On Vercel the execution environment is frozen the moment the response is written,
# so a ``threading.Thread`` started during a request may resume minutes later or
# never - and it fails silently, only in production.  Delivering synchronously
# instead would put the consumer's round-trip on the critical path of every portal
# page load, including the 40ms task-toggle endpoint.
#
# So: the outbox is drained out-of-band by ``POST /api/v1/webhooks/drain``, called
# by Vercel Cron and/or by the consuming VM on whatever schedule it likes.  Delivery
# latency equals the drain interval, which the consumer controls.
# ==========================================================================

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta

log = logging.getLogger("viama.api.webhooks")

#: Attempt N waits this long before being retried.
BACKOFF_MINUTES = (1, 5, 30, 120, 360, 1440)

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0


def max_attempts():
    try:
        return int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "7"))
    except ValueError:
        return 7


def sign(secret, timestamp, body):
    """
    ``X-Viama-Signature: t=<unix>,v1=<hex>``

    HMAC over ``"{timestamp}.{body}"`` rather than the body alone, so a captured
    payload cannot be replayed later with a fresh timestamp.
    """
    payload = f"{timestamp}.{body}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret, header, body, tolerance_seconds=300):
    """Reference verifier - mirror this on the receiving side."""
    try:
        parts = dict(piece.split("=", 1) for piece in header.split(","))
        timestamp = int(parts["t"])
        supplied = parts["v1"]
    except Exception:
        return False, "malformed signature header"

    if abs(time.time() - timestamp) > tolerance_seconds:
        return False, "timestamp outside tolerance (check NTP on both machines)"

    expected = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return False, "signature mismatch"
    return True, None


def deliver_one(endpoint, outbox_row, attempt):
    """
    POST one event to one endpoint.

    Returns ``(ok, status_code, error, latency_ms)``.  Never raises - a delivery
    failure must not affect anything else.
    """
    import requests

    body = json.dumps(outbox_row.payload, default=str, separators=(",", ":"))
    timestamp = int(time.time())

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "viama-webhooks/1.0",
        "X-Viama-Signature": sign(endpoint.secret, timestamp, body),
        "X-Viama-Event": outbox_row.event,
        "X-Viama-Delivery": str(outbox_row.id),
        "X-Viama-Attempt": str(attempt),
        "X-Viama-Api-Version": "v1",
    }

    started = time.perf_counter()
    try:
        response = requests.post(
            endpoint.url,
            data=body.encode(),
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        latency = int((time.perf_counter() - started) * 1000)
        if 200 <= response.status_code < 300:
            return True, response.status_code, None, latency
        return (
            False,
            response.status_code,
            f"HTTP {response.status_code}: {response.text[:300]}",
            latency,
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return False, None, f"{type(exc).__name__}: {exc}"[:300], latency


def _next_retry(attempt):
    index = min(attempt - 1, len(BACKOFF_MINUTES) - 1)
    return datetime.utcnow() + timedelta(minutes=BACKOFF_MINUTES[index])


def drain_outbox(limit=50, endpoint_id=None):
    """
    Deliver pending events. Safe to call concurrently and repeatedly.

    Returns a summary dict suitable for logging on the caller's side.
    """
    from extensions import db

    from core.models import WebhookDelivery, WebhookEndpoint, WebhookOutbox

    now = datetime.utcnow()

    endpoints = WebhookEndpoint.query.filter_by(active=True)
    if endpoint_id:
        endpoints = endpoints.filter_by(id=endpoint_id)
    endpoints = endpoints.all()

    if not endpoints:
        return {
            "attempted": 0,
            "delivered": 0,
            "failed": 0,
            "pending_remaining": WebhookOutbox.query.filter_by(status="pending").count(),
            "note": "No active webhook endpoints registered.",
        }

    rows = (
        WebhookOutbox.query.filter(
            WebhookOutbox.status == "pending",
            db.or_(
                WebhookOutbox.next_retry_at.is_(None),
                WebhookOutbox.next_retry_at <= now,
            ),
        )
        .order_by(WebhookOutbox.id.asc())
        .limit(limit)
        .all()
    )

    attempted = delivered = failed = 0
    ceiling = max_attempts()

    for row in rows:
        targets = [e for e in endpoints if e.wants(row.event)]
        if not targets:
            # Nobody is subscribed - resolve it rather than retrying forever.
            row.status = "skipped"
            row.delivered_at = datetime.utcnow()
            row.last_error = "no subscribed endpoint"
            continue

        attempt = (row.attempts or 0) + 1
        row.attempts = attempt
        attempted += 1
        all_ok = True
        last_failure = None

        for endpoint in targets:
            success, status_code, error, latency = deliver_one(endpoint, row, attempt)

            db.session.add(
                WebhookDelivery(
                    outbox_id=row.id,
                    endpoint_id=endpoint.id,
                    event=row.event,
                    attempt=attempt,
                    status="delivered" if success else "failed",
                    response_status=status_code,
                    latency_ms=latency,
                    error=error,
                )
            )

            if success:
                endpoint.last_success_at = datetime.utcnow()
                endpoint.failure_count = 0
            else:
                all_ok = False
                last_failure = error
                endpoint.last_error_at = datetime.utcnow()
                endpoint.last_error = error
                endpoint.failure_count = (endpoint.failure_count or 0) + 1

        if all_ok:
            row.status = "delivered"
            row.delivered_at = datetime.utcnow()
            row.last_error = None
            delivered += 1
        elif attempt >= ceiling:
            # Dead-letter: keep the row for inspection and manual retry.
            row.status = "failed"
            row.last_error = f"gave up after {attempt} attempts: {last_failure}"
            failed += 1
        else:
            row.next_retry_at = _next_retry(attempt)
            # Record why, so an operator inspecting the outbox can see the cause
            # without cross-referencing the deliveries table.
            row.last_error = last_failure
            failed += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("failed to record drain results")

    return {
        "attempted": attempted,
        "delivered": delivered,
        "failed": failed,
        "endpoints": len(endpoints),
        "pending_remaining": WebhookOutbox.query.filter_by(status="pending").count(),
    }


def send_test(endpoint, event="ping"):
    """Fire a synthetic event at one endpoint, for setup verification."""
    import uuid

    from core.config import ist_now_aware, iso, utc_now

    class _Fake:
        id = 0

    fake = _Fake()
    fake.event = event
    fake.payload = {
        "id": uuid.uuid4().hex,
        "event": event,
        "occurred_at_utc": iso(utc_now().replace(tzinfo=None)) + "Z",
        "occurred_at_ist": iso(ist_now_aware()),
        "api_version": "v1",
        "source": "api",
        "actor": {"type": "test"},
        "data": {
            "object": "test",
            "message": "If you can read this and the signature verified, you are set up.",
        },
    }

    success, status_code, error, latency = deliver_one(endpoint, fake, 1)
    return {
        "delivered": success,
        "status_code": status_code,
        "latency_ms": latency,
        "error": error,
    }

