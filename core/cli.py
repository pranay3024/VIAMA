"""
Command line for API administration.

    python -m core.cli gen-secret
    python -m core.cli create-tables
    python -m core.cli migrate
    python -m core.cli mint-token --sub other-vm --scopes '*' --name "Mirror site"
    python -m core.cli list-tokens
    python -m core.cli revoke-token --jti <jti>
    python -m core.cli add-webhook --url https://other-vm/hooks/viama
    python -m core.cli list-webhooks
    python -m core.cli drain
    python -m core.cli receiver --port 9000

Everything runs inside the real Flask app context, so it uses the same
DATABASE_URL the portal does.
"""

import argparse
import json
import secrets
import sys


def _app():
    from app import create_app

    return create_app()


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def cmd_gen_secret(args):
    print("Add these to your environment (Vercel dashboard, or .env locally):\n")
    print(f"API_JWT_SECRET={secrets.token_urlsafe(48)}")
    print(f"CRON_SECRET={secrets.token_urlsafe(32)}")
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(
        "\nAPI_JWT_SECRET signs API tokens. Rotating it invalidates every token."
        "\nSECRET_KEY is the Flask session key - it is currently hardcoded as"
        "\n'viama_secret' in both config.py:7 and app.py:14 (line 14 wins)."
    )
    return 0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def cmd_create_tables(args):
    """Create the API's own tables. Existing portal tables are left alone."""
    from extensions import db

    import core.models as api_models  # noqa: F401  (registers the models)

    app = _app()
    with app.app_context():
        wanted = [model.__table__ for model in api_models.API_TABLES]
        db.metadata.create_all(bind=db.engine, tables=wanted, checkfirst=True)
        print("Created/verified:")
        for table in wanted:
            print(f"  - {table.name}")
    return 0


#: Additive DDL applied inside a transaction.
#:
#: This used to add a ``deleted_at`` column to surveys / survey_assignments /
#: users.  Nothing ever read it: soft delete writes a tombstone to
#: ``api_deleted_records`` instead, precisely so the portal does not need a
#: two-phase deploy (see core/models.py:DeletedRecord).  The columns were dead
#: weight and the "run this before deploying" warning they carried was scaring
#: operators away from an otherwise index-only migration, so they are gone.
#:
#: If a previous run already added them they are harmless - leave them.
MIGRATION_STATEMENTS = []

#: CONCURRENTLY cannot run inside a transaction, so these are applied with
#: AUTOCOMMIT. They speed up the existing portal too - it currently runs
#: filter_by(section_no=...) inside a loop over every survey.
INDEX_STATEMENTS = [
    ("ix_surveys_section_no", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_surveys_section_no ON surveys (section_no)"),
    ("ix_surveys_status", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_surveys_status ON surveys (status)"),
    ("ix_surveys_start_time", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_surveys_start_time ON surveys (start_time)"),
    ("ix_surveys_captain_email", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_surveys_captain_email ON surveys (captain_email)"),
    ("ix_assign_section_no", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_assign_section_no ON survey_assignments (section_no)"),
    ("ix_assign_day_enabled", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_assign_day_enabled ON survey_assignments (survey_day, survey_enabled)"),
]


def cmd_migrate(args):
    from sqlalchemy import text

    from extensions import db

    app = _app()
    with app.app_context():
        if MIGRATION_STATEMENTS:
            with db.engine.begin() as conn:
                for label, statement in MIGRATION_STATEMENTS:
                    conn.execute(text(statement))
                    print(f"  ok  {label}")

        if not args.skip_indexes:
            autocommit = db.engine.execution_options(isolation_level="AUTOCOMMIT")
            with autocommit.connect() as conn:
                for label, statement in INDEX_STATEMENTS:
                    try:
                        conn.execute(text(statement))
                        print(f"  ok  {label}")
                    except Exception as exc:
                        print(f"  !!  {label}: {exc}")

    print(
        "\nDone. These are additive and index-only - safe to run at any time, "
        "\nbefore or after a deploy, with the portal live."
    )
    return 0


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def cmd_mint_token(args):
    from core.engine import ALL_SCOPES, READ_ONLY_SCOPES, mint_token

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    if scopes == ["readonly"]:
        scopes = list(READ_ONLY_SCOPES)

    unknown = [s for s in scopes if s != "*" and not s.endswith(":*") and s not in ALL_SCOPES]
    if unknown:
        print(f"Unknown scope(s): {unknown}", file=sys.stderr)
        print(f"Valid scopes: {list(ALL_SCOPES)}", file=sys.stderr)
        return 1

    app = _app()
    with app.app_context():
        token, record = mint_token(
            subject=args.sub,
            scopes=scopes,
            name=args.name or args.sub,
            note=args.note,
            created_by="cli",
        )

    if args.quiet:
        print(token)
        return 0

    print("Token created. It is shown ONCE - store it now.\n")
    print(token)
    print(f"\n  sub    : {args.sub}")
    print(f"  jti    : {record.jti}")
    print(f"  scopes : {', '.join(scopes)}")
    print("\nUse it as:")
    print('  curl -H "Authorization: Bearer <token>" https://<portal>/api/v1/auth/whoami')
    print(f"\nTo revoke: python -m core.cli revoke-token --jti {record.jti}")
    return 0


def cmd_list_tokens(args):
    from core.models import ApiToken

    app = _app()
    with app.app_context():
        rows = ApiToken.query.order_by(ApiToken.id).all()
        if not rows:
            print("No tokens issued.")
            return 0
        print(f"{'ID':<4} {'NAME':<24} {'KIND':<9} {'REVOKED':<8} {'LAST USED':<20} JTI")
        for row in rows:
            last = row.last_used_at.strftime("%Y-%m-%d %H:%M:%S") if row.last_used_at else "-"
            print(
                f"{row.id:<4} {(row.name or '')[:23]:<24} {(row.kind or ''):<9} "
                f"{str(bool(row.revoked)):<8} {last:<20} {row.jti}"
            )
    return 0


def cmd_revoke_token(args):
    from extensions import db

    from core.models import ApiToken

    app = _app()
    with app.app_context():
        query = ApiToken.query
        row = query.filter_by(jti=args.jti).first() if args.jti else query.get(args.id)
        if not row:
            print("Token not found.", file=sys.stderr)
            return 1
        row.revoked = True
        db.session.commit()
        print(f"Revoked token {row.id} ({row.name}).")
        print(
            "Warm instances may accept it for up to API_TOKEN_CACHE_TTL seconds "
            "(default 30). Set API_TOKEN_CACHE_TTL=0 for immediate effect."
        )
    return 0


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def cmd_add_webhook(args):
    from extensions import db

    from core.models import WebhookEndpoint

    app = _app()
    with app.app_context():
        endpoint = WebhookEndpoint(
            url=args.url,
            secret=args.secret or secrets.token_urlsafe(32),
            events=args.events,
            active=True,
            description=args.description,
        )
        db.session.add(endpoint)
        db.session.commit()
        print(f"Endpoint {endpoint.id} -> {endpoint.url}")
        print(f"  events : {endpoint.events}")
        print(f"  secret : {endpoint.secret}")
        print("\nVerify deliveries with this secret:")
        print("  signature = HMAC_SHA256(secret, f'{t}.{raw_body}')")
        print("  header    = X-Viama-Signature: t=<unix>,v1=<hex>")
    return 0


def cmd_list_webhooks(args):
    from core.models import WebhookEndpoint

    app = _app()
    with app.app_context():
        rows = WebhookEndpoint.query.order_by(WebhookEndpoint.id).all()
        if not rows:
            print("No webhook endpoints registered.")
            return 0
        for row in rows:
            print(f"[{row.id}] {row.url}")
            print(f"     active={row.active} events={row.events} failures={row.failure_count}")
            if row.last_error:
                print(f"     last error: {row.last_error[:120]}")
    return 0


def cmd_drain(args):
    from core.engine import drain_outbox

    app = _app()
    with app.app_context():
        result = drain_outbox(limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0


def cmd_receiver(args):
    """
    Minimal webhook receiver for local testing.

    Verifies the HMAC signature and prints each event, so you can prove delivery
    works without involving the second VM.
    """
    import hashlib
    import hmac as hmac_mod

    from flask import Flask, request

    receiver = Flask("viama-webhook-receiver")
    secret = args.secret

    @receiver.post("/")
    @receiver.post("/<path:anything>")
    def receive(anything=None):
        raw = request.get_data()
        header = request.headers.get("X-Viama-Signature", "")
        verdict = "UNVERIFIED (no --secret given)"
        if secret:
            try:
                parts = dict(piece.split("=", 1) for piece in header.split(","))
                expected = hmac_mod.new(
                    secret.encode(), f"{parts['t']}.{raw.decode()}".encode(), hashlib.sha256
                ).hexdigest()
                verdict = "VALID" if hmac_mod.compare_digest(expected, parts["v1"]) else "INVALID"
            except Exception as exc:
                verdict = f"MALFORMED ({exc})"

        body = request.get_json(silent=True) or {}
        print("-" * 70)
        print(f"event      : {body.get('event')}")
        print(f"signature  : {verdict}")
        print(f"entity     : {body.get('data', {}).get('object')} #{body.get('data', {}).get('id')}")
        print(f"changed    : {json.dumps(body.get('data', {}).get('changed'))}")
        return {"received": True}, 200

    print(f"Listening on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    receiver.run(port=args.port, debug=False)
    return 0


# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m core.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gen-secret", help="generate API_JWT_SECRET / CRON_SECRET / SECRET_KEY")
    sub.add_parser("create-tables", help="create the API's own tables")

    migrate = sub.add_parser("migrate", help="additive DDL: performance indexes")
    migrate.add_argument("--skip-indexes", action="store_true")

    mint = sub.add_parser("mint-token", help="issue an API token")
    mint.add_argument("--sub", required=True, help="subject, e.g. 'other-vm'")
    mint.add_argument("--scopes", default="*", help="csv, '*', or 'readonly'")
    mint.add_argument("--name")
    mint.add_argument("--note")
    mint.add_argument("--quiet", action="store_true", help="print only the token")

    sub.add_parser("list-tokens", help="list issued tokens")

    revoke = sub.add_parser("revoke-token", help="revoke a token")
    revoke.add_argument("--jti")
    revoke.add_argument("--id", type=int)

    add_hook = sub.add_parser("add-webhook", help="register a webhook endpoint")
    add_hook.add_argument("--url", required=True)
    add_hook.add_argument("--secret")
    add_hook.add_argument("--events", default="*")
    add_hook.add_argument("--description")

    sub.add_parser("list-webhooks", help="list webhook endpoints")

    drain = sub.add_parser("drain", help="deliver pending webhook events now")
    drain.add_argument("--limit", type=int, default=50)

    receiver = sub.add_parser("receiver", help="run a local webhook receiver for testing")
    receiver.add_argument("--port", type=int, default=9000)
    receiver.add_argument("--secret", help="endpoint secret, to verify signatures")

    return parser


COMMANDS = {
    "gen-secret": cmd_gen_secret,
    "create-tables": cmd_create_tables,
    "migrate": cmd_migrate,
    "mint-token": cmd_mint_token,
    "list-tokens": cmd_list_tokens,
    "revoke-token": cmd_revoke_token,
    "add-webhook": cmd_add_webhook,
    "list-webhooks": cmd_list_webhooks,
    "drain": cmd_drain,
    "receiver": cmd_receiver,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
