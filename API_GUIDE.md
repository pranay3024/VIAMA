# VIAMA API — integration guide

Everything the portal holds is reachable over HTTP: raw records, the values the
dashboards compute at render time, full-database dumps, access logs, and a push
channel. Built additively — no existing route, template or model was changed.

---

## 1. Files

The whole API is six files inside `core/`:

| File | What it is |
|---|---|
| `core/config.py` | Constants and derived-value logic (project weeks, team→state map, upload durations, checklists) |
| `core/models.py` | The API's own tables — tokens, webhooks, change log, audit log |
| `core/engine.py` | Auth, errors, query parsing, serialization, CRUD, workflow, audit, webhook delivery |
| `core/endpoints.py` | Every route |
| `core/api.py` | Mounting, stealth mode, integrity check |
| `core/cli.py` | Admin commands |
| `core/dump_client.py` | Standalone — **copy this one to the other VM** |

`app.py` gained three lines. Nothing else in the project was touched.

**If any of these files is deleted the app refuses to start**, with a message
naming the missing file. That is deliberate: a silently-skipped module is how a
deletion turns into "the other site stopped receiving data" a week later. Set
`API_REQUIRED=0` if you would rather the portal boot without its API.

> **This project is not under version control.** That is the real reason a
> deleted file was unrecoverable last time. `git init`, commit, and push to a
> private remote — do this before anything else. A `.gitignore` is included that
> keeps `.env`, `client_secret.json` and both token pickles out of history.

---

## 2. Setup

```bash
# 1. generate secrets  (prints API_JWT_SECRET, CRON_SECRET, SECRET_KEY)
python -m core.cli gen-secret

# 2. put them in the environment (Vercel dashboard, or .env locally)

# 3. create the API's tables — additive, existing tables untouched
python -m core.cli create-tables

# 4. mint the token for the other VM
python -m core.cli mint-token --sub other-vm --scopes '*' --name "Mirror site"
```

The token is shown **once**. To revoke it:
`python -m core.cli revoke-token --jti <jti>` — effective within
`API_TOKEN_CACHE_TTL` seconds (default 30; set `0` for immediate).

Optional but recommended — indexes that speed up the existing portal too:

```bash
python -m core.cli migrate
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `API_JWT_SECRET` | — | **Required.** Signs API tokens. Must not reuse the Flask key. |
| `API_BASE_PATH` | `/api/v1` | Move the API to an unguessable prefix |
| `API_STEALTH` | `0` | `1` = unauthenticated requests get a bare 404 |
| `API_REQUIRED` | `1` | `0` = boot the portal even if API files are missing |
| `API_ENABLED` | `1` | `0` = every API route returns 503 |
| `API_TOKEN_CACHE_TTL` | `30` | Revocation lag, seconds |
| `API_CORS_ORIGINS` | *(empty)* | Comma-separated exact origins |
| `CRON_SECRET` | — | Auth for `/jobs/tick` |
| `AUDIT_LOG_REQUESTS` | `all` | `all` / `api` / `mutations` / `off` |
| `AUDIT_RETENTION_DAYS` | `365` | Audit rows older than this are purged |
| `AUDIT_ANONYMISE_IP` | `0` | `1` = store masked IPs |
| `WEBHOOK_ENABLED_EVENTS` | *(sensible default)* | `*` for everything |
| `SYNC_SAFE_LAG_SECONDS` | `5` | Change-feed visibility lag |

---

## 3. Authentication

One long-lived bearer token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://<portal>/api/v1/auth/whoami
```

Scopes: `surveys:read|write`, `users:*`, `assignments:*`, `dashboards:read`,
`reports:read`, `exports:read`, `actions:write`, `media:write`, `alerts:*`,
`audit:read`, `dump:read`, `webhooks:admin`, `jobs:run`, `admin:destroy`.
Wildcards `resource:*` and `*` work.

`audit:read` is **excluded** from the read-only bundle on purpose — access logs
contain IP addresses, so a mirror token must be granted it deliberately.

### Hiding the API

```bash
API_BASE_PATH=/_int/7f3a91     # API is no longer at /api/v1
API_STEALTH=1                  # unauthenticated requests get a plain 404
```

With stealth on, a probe sees no 401, no `WWW-Authenticate`, and no error code
naming the service. A caller holding a valid token is unaffected, and real
validation errors keep their detail.

Neither setting is a security control — the token is. They keep the API off the
radar of people poking at the site; they do not protect it. The `.py` files
themselves are never served over HTTP by Flask.

---

## 4. Response shape

```json
{
  "data": { },
  "meta": {
    "request_id": "…",
    "generated_at_utc": "…",
    "pagination": { "page": 1, "per_page": 50, "total": 812, "has_next": true }
  },
  "links": { "self": "…", "next": "…" }
}
```

Errors:

```json
{ "error": { "code": "invalid_state_transition", "message": "…", "status": 409,
             "details": [ { "field": "status", "issue": "illegal transition" } ] } }
```

### Timestamps — read this before using any date

The database is internally inconsistent, so every timestamp is returned three ways:

```json
{ "start_time_utc": "2026-07-28T04:30:00Z",
  "start_time_ist": "2026-07-28T10:00:00+05:30",
  "start_time_raw": "2026-07-28T10:00:00" }
```

* **`*_utc` is the real instant — use this.**
* `*_raw` is the naive value exactly as stored.
* `surveys.start_time` / `end_time` store **IST wall-clock**; everything else
  stores **UTC**. `GET /meta/version` documents this per column.

Dashboard endpoints additionally return `display_start_time` /
`display_end_time`, which reproduce **what the portal shows on screen** — the
portal adds +5:30 to a column that is already IST, so those values are 5h30m
ahead of reality. That is a pre-existing portal bug, preserved so your screens
can match. Use `*_utc` for anything real.

---

## 5. Querying

```
?status=completed&state__in=ODISHA,BIHAR&week=6&sort=-start_time&page=2&per_page=100
```

Operators: `__in __ne __gt __gte __lt __lte __like __startswith __isnull`.
Search: `?q=text`. Compound filters: `week`, `from_date`, `to_date`,
`survey_date`, `team`, `stretch`, `has_pdf`, `has_pending_task`.

Sort presets reproducing each dashboard's exact row order: `admin_rank`,
`regional_rank`, `teamleader_rank`, `roadvision_rank`, `day_order`.

Unknown query parameters return **422**, not silence — a typo like `?statuss=`
would otherwise return the whole unfiltered table and look like a data bug.

---

## 6. What's available

Full listing: `GET /meta/endpoints` · machine-readable: `GET /openapi.json`

| Area | Highlights |
|---|---|
| **Records** | `/surveys` `/users` `/assignments` `/schedules` `/equipment` `/regional-manager-states` — full CRUD |
| **Workflow** | `/surveys/start` · `/groundwork-complete` · `/complete` · `/video-counts` · `/roadvision-review` · `/tasks/{task1\|task2\|survey_form}` · resurvey + PDF re-upload |
| **Dashboards** | `/dashboards/{admin,regional,teamleader,roadvision,captain,backup}` — the exact payload each page renders |
| **Reports** | `/reports/{summary,team-km,weekly,captain-performance,upload-sla}` |
| **Exports** | `/exports/surveys.xlsx` (byte-compatible with the portal's export), `.csv`, assignments |
| **Audit** | `/audit/{logins,requests,sessions,failed-logins,active-users,ips,summary}` |
| **Dump** | `/dump` `/dump/{table}` `/dump/manifest` `/dump/{table}/checksum` |
| **Push** | `/webhooks/*` · `/sync/changes` |
| **Meta** | `/meta/{weeks,teams,states,statuses,checklists,defect-fields,filters,integrity}` |

`GET /meta/checklists` returns the 20-item pre-survey and 7-item recording
checklists — they only exist as hardcoded HTML in the portal templates.

---

## 7. Daily database dump

Copy `core/dump_client.py` to the other VM. It needs only the Python 3 standard
library.

```cron
0 12 * * * /usr/bin/python3 /opt/viama/dump_client.py \
    --url https://your-portal.vercel.app \
    --token "$VIAMA_API_TOKEN" \
    --out /var/backups/viama \
    --keep-days 30 >> /var/log/viama-dump.log 2>&1
```

It fetches a manifest, streams the gzipped dump to disk, **verifies the row
counts**, records a cursor for incremental runs, and prunes old files. It exits
non-zero on any failure — a backup job that fails silently is worse than none.

Flags: `--incremental` (only new rows), `--include-audit` (access logs, contains
IPs), `--no-compress`.

Or by hand:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
     "https://<portal>/api/v1/dump?compress=gzip" \
     -o "viama_$(date +%F).ndjson.gz"
```

The dump is NDJSON: a `_meta` line, one line per row tagged with `_table`, then a
`_summary` line with per-table counts. **The `_summary` line is the proof the
download completed** — a truncated file simply will not have it.

Per-table dumps also support `?format=csv|sql|json`.

---

## 8. Push (webhooks)

```bash
python -m core.cli add-webhook --url https://other-vm/hooks/viama --events '*'
```

Every change — through the API **or** through the portal UI — is captured by an
ORM-level hook and written to an outbox inside the same transaction, so nothing
is lost. Delivery happens when the outbox is drained:

```cron
* * * * * curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" \
    https://<portal>/api/v1/jobs/tick
```

Delivery latency equals your drain interval — you control it. Delivery is *not*
inline because on Vercel the execution environment is frozen the moment a
response is sent, so a background thread silently dies in production only.

Payloads are signed:

```
X-Viama-Signature: t=<unix>,v1=<hex hmac_sha256(secret, "<t>.<raw body>")>
```

Reject if `|now - t| > 300`. **Run NTP on the receiver** — clock skew is the
usual cause of verification failures. Test locally with
`python -m core.cli receiver --port 9000 --secret <secret>`.

Events include `survey.created`, `survey.completed`, `survey.roadvision_reviewed`,
`survey.task_toggled`, `assignment.missed`, `auth.login_success`,
`auth.login_failed`, `auth.logout`. Full catalog: `GET /webhooks/events`.

### Catch-up feed

If a webhook is missed, replay from a cursor:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     "https://<portal>/api/v1/sync/changes?since=$CURSOR"
```

Store `next_cursor` and pass it back. The feed is intentionally ~5s behind: a
sequence value is allocated at INSERT but only visible at COMMIT, so without a
lag a poller would permanently skip rows that committed out of order.

---

## 9. Access logs

Every login, failed login and logout is recorded with IP (real client IP from
`X-Forwarded-For`, not the proxy), user agent, device/browser/OS, edge-provided
geo, and a session id linking the login to every request that followed. Captured
by an app-level hook — `routes/auth.py` was not modified.

```bash
curl -H "Authorization: Bearer $TOKEN" "https://<portal>/api/v1/audit/logins?days=7"
curl -H "Authorization: Bearer $TOKEN" "https://<portal>/api/v1/audit/failed-logins"
curl -H "Authorization: Bearer $TOKEN" "https://<portal>/api/v1/audit/sessions"
```

`/audit/requests` is the full access log. Volume is controlled by
`AUDIT_LOG_REQUESTS`; rows are purged after `AUDIT_RETENTION_DAYS` by
`/jobs/tick`.

IP addresses are personal data in most jurisdictions. Keep the retention window
defensible, or set `AUDIT_ANONYMISE_IP=1`.

---

## 10. Monitoring

```bash
GET  /api/v1/meta/health       # no auth — DB reachability
GET  /api/v1/meta/integrity    # no auth — are the API's own files intact?
GET  /api/v1/webhooks/health   # backlog and endpoint failures
GET  /api/v1/sync/status?since=<cursor>   # replica lag
```

**Poll `/meta/integrity` from the other VM.** It goes `ok: false` the moment a
core file is missing, instead of the integration failing quietly.

---

## 11. Before this goes public

The API is a permanent credential pointed at a database whose password is
currently in plaintext in this directory. In priority order:

1. **`git init` and push to a private remote.** Nothing else here matters if the
   code cannot be restored. `.gitignore` is in place and excludes `.env`,
   `client_secret.json` and both token pickles — commit it *first*.
2. **Reset the Supabase Postgres password** and update `DATABASE_URL`. It is in
   plaintext in `.env` and in this repository's history.
3. **Rotate the Supabase anon key.** It is now read from `SUPABASE_ANON_KEY`
   (`supabase_client.py`), so rotating is a config change rather than a code
   change — but the old key is still public and still works until you rotate it.
   Audit the RLS policies on the `survey-pdfs` bucket at the same time:
   `captain.py` uses this key to upload *and delete* PDFs, so if the bucket is
   anon-writable, that leaked key can delete every survey PDF.
4. **Replace `token_drive.pickle`** with a Google service account. Set
   `GOOGLE_SA_JSON` and `google_drive.py` uses it instead — no refresh token on
   disk, and that pickle no longer has to ship in the deployment bundle.
   `token_gmail.pickle` must still ship: Gmail drafts need a real mailbox, which
   a service account is not. Both are `pickle.load`ed when not overridden, which
   is arbitrary code execution if either file is ever swapped.

   Drive and Gmail now resolve **independently**, so an expired Gmail token no
   longer breaks Drive uploads. Regenerate them with `python generate_token.py`
   and `python generate_gmail_token.py` respectively.

**Already done** (see `CHANGES_VS_BASE.md`):

- `SECRET_KEY` is read from the environment. It used to be the literal
  `"viama_secret"` in *both* `config.py` and `app.py`, with the `app.py`
  assignment silently winning — so changing only one did nothing. That
  assignment is gone and there is one source of truth.
- `API_JWT_SECRET` is set, so authenticated calls work. When it is missing the
  API now answers `503 api_misconfigured` with the remedy instead of a bare 500.

---

## 12. Known limits

1. `GET /admin` and `GET /regional` still mutate data as a side effect of
   rendering. The API never does, so the two can briefly disagree on the missed
   count if nobody has loaded `/admin` recently.
2. Timezone semantics were inferred from how the code writes each column. Verify
   against one survey whose real start time you know before trusting historical
   `*_utc` values. `GET /meta/version` reports which columns are `assumed`.
3. Lists are capped at 500 rows per page (dashboards default 200). The portal
   pages return everything, so reproducing a full page may need `links.next`.
4. When Drive credentials expire, every media endpoint returns 503 until they
   are refreshed. `GET /media/status` now builds the client for real, so it
   fails there for the same reasons an upload would rather than reporting a
   false "available".
5. `core/config.py` duplicates business rules that still live inline in
   `routes/*.py`. Run `python check_config_drift.py` before any deploy that
   touched a route file — it fails if the two have diverged on the project
   epoch, export columns, statuses, states or roles.

**Fixed since the first draft of this guide:**

- Bulk `Query.update()` in `admin.py` no longer goes unreported. Acknowledge-alert
  iterates its rows so each emits a proper event, and the Monday reset keeps its
  bulk statement but calls `record_bulk_change()` to announce itself as
  `assignment.bulk_changed`.
- Soft-deleted records are hidden from the roadvision and captain screens too,
  via `utils.visibility.exclude_deleted`, which filters on the tombstone rather
  than on the dashboard flags.
- Soft delete now emits `*.deleted`. It previously looked for a `deleted_at`
  column that does not exist on these models, so a soft delete reached consumers
  as a meaningless `*.updated`.
