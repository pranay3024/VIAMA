"""
VIAMA - API mounting, obscurity and integrity.

``register_api(app)`` is the only thing app.py calls.  It mounts every route,
installs the JSON error handlers, applies CORS, and attaches change capture and
audit logging.

The portal is untouched: every URL lives under the configured base path, the
error handlers ignore non-API paths, and the API never reads or writes
``flask.session``.

Two protections worth understanding
-----------------------------------
**Fail loudly, not silently.**  This used to log a warning and carry on when a
module was missing - which is exactly how a deleted file becomes "the other site
quietly stopped receiving data" a week later, with nothing in the logs pointing
at the cause.  A missing or broken module now raises at start-up.  Set
``API_REQUIRED=0`` if you would rather the portal boot without its API.

**Obscurity on top of auth, never instead of it.**  ``API_BASE_PATH`` moves the
whole API to an unguessable prefix and ``API_STEALTH=1`` answers unauthenticated
requests with a plain 404, so probing reveals nothing.  Neither is a security
control - the bearer token is.  They just keep the API off the radar.
"""

import importlib
import logging
import os

from flask import Blueprint, jsonify, request

from core.config import MAX_UPLOAD_BYTES

log = logging.getLogger("viama.api")

API_VERSION = "v1"
DEFAULT_BASE_PATH = f"/api/{API_VERSION}"

#: Losing any of these breaks the integration, so absence is an error.
REQUIRED_MODULES = ("core.config", "core.models", "core.engine", "core.endpoints")

#: Modules providing a ``bp`` blueprint.
ROUTE_MODULES = ("core.endpoints",)


class ApiIntegrityError(RuntimeError):
    """A required API module is missing or broken."""


def base_path():
    """
    Where the API is mounted. Default ``/api/v1``.

    Set ``API_BASE_PATH`` to something unguessable (e.g. ``/_int/7f3a91``) and
    the API disappears from casual view; the consuming VM just uses the same
    prefix.
    """
    raw = (os.getenv("API_BASE_PATH") or DEFAULT_BASE_PATH).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or DEFAULT_BASE_PATH


def stealth_enabled():
    """
    Answer unauthenticated API requests with a bare 404.

    Someone who finds the URL cannot then tell an API from a typo: no 401, no
    ``WWW-Authenticate``, no error code naming the service.  A caller holding a
    valid token sees entirely normal behaviour.
    """
    return os.getenv("API_STEALTH", "0").strip().lower() in ("1", "true", "yes")


def api_required():
    return os.getenv("API_REQUIRED", "1").strip().lower() not in ("0", "false", "no")


def _cors_origins():
    raw = os.getenv("API_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def check_integrity():
    """
    Confirm every required module is present and importable.

    Also served at ``<base>/meta/integrity`` so the consuming VM can alarm on a
    deleted file instead of discovering it through missing data.
    """
    report = {"ok": True, "modules": {}, "missing": [], "broken": []}

    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
            report["modules"][name] = {
                "present": True,
                "file": getattr(module, "__file__", None),
            }
        except ModuleNotFoundError as exc:
            report["ok"] = False
            report["missing"].append(name)
            report["modules"][name] = {"present": False, "error": str(exc)}
        except Exception as exc:
            report["ok"] = False
            report["broken"].append(name)
            report["modules"][name] = {
                "present": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    if not report["ok"]:
        report["remedy"] = (
            "A core API file is missing or broken. Restore core/ from version "
            "control and redeploy. Required: " + ", ".join(REQUIRED_MODULES)
        )
    return report


def create_api_blueprint():
    """Build the API blueprint with every route mounted."""
    api = Blueprint("api_root", __name__, url_prefix=base_path())

    for path in ROUTE_MODULES:
        try:
            module = importlib.import_module(path)
        except Exception as exc:
            if api_required():
                raise ApiIntegrityError(
                    f"Cannot import {path}: {exc}. The API cannot start. Restore "
                    f"core/ and redeploy, or set API_REQUIRED=0 to boot the "
                    f"portal without its API."
                ) from exc
            log.error("api: %s failed to import (%s) - API DISABLED", path, exc)
            continue

        blueprint = getattr(module, "bp", None)
        if blueprint is None:
            raise ApiIntegrityError(f"{path} defines no 'bp' blueprint.")
        api.register_blueprint(blueprint)

    return api


def register_api(app):
    """Mount the API onto an existing Flask app. Call once, from create_app()."""
    from core.engine import register_error_handlers

    report = check_integrity()
    if not report["ok"] and api_required():
        raise ApiIntegrityError(report["remedy"])

    mount = base_path()
    app.register_blueprint(create_api_blueprint())
    register_error_handlers(app)

    app.config["VIAMA_API_BASE_PATH"] = mount
    app.config["VIAMA_API_STEALTH"] = stealth_enabled()

    origins = _cors_origins()

    @app.before_request
    def _guard_api_upload_size():
        if not request.path.startswith(mount):
            return None
        length = request.content_length
        if length and length > MAX_UPLOAD_BYTES:
            from core.engine import PayloadTooLarge

            raise PayloadTooLarge(
                message=f"Request body exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            )
        return None

    @app.after_request
    def _apply_api_cors(response):
        if not request.path.startswith(mount):
            return response
        origin = request.headers.get("Origin")
        if origin and origin in origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-Api-Key, X-Request-Id, Idempotency-Key"
            )
            response.headers["Access-Control-Expose-Headers"] = (
                "X-Request-Id, X-Total-Count, Idempotency-Replayed"
            )
            response.headers["Access-Control-Max-Age"] = "600"
            # Bearer tokens do not need cookies; keep credentials off.
            response.headers["Access-Control-Allow-Credentials"] = "false"
        return response

    if stealth_enabled():

        @app.after_request
        def _stealth(response):
            """
            Turn auth failures into an indistinguishable 404.

            API paths only, auth failures only - a valid token is unaffected and
            genuine validation errors keep their detail.
            """
            if not request.path.startswith(mount):
                return response
            if response.status_code in (401, 403):
                blank = jsonify({"error": {"code": "not_found", "status": 404}})
                blank.status_code = 404
                blank.headers.pop("WWW-Authenticate", None)
                return blank
            return response

    # Change capture -> webhook outbox + change log.
    try:
        from core.engine import install_hooks

        install_hooks(app)
    except Exception:
        log.exception("api: change-capture hooks failed to install")

    # Access + authentication auditing. Watches the session at app level, so
    # routes/auth.py needs no changes to have logins recorded.
    try:
        from core.engine import install_audit

        install_audit(app)
    except Exception:
        log.exception("api: audit hooks failed to install")

    log.info("viama: API mounted at %s (stealth=%s)", mount, stealth_enabled())
    return app
