# VIAMA Mirror Site — Complete Build Brief

**Hand this entire file to whoever (or whatever) builds the second website.**
It contains every endpoint, every field name, every page to build, and every
trap in the data. It is generated from the live API — 182 routes — not from
memory.

---

## 0. THE TASK

Build a web application that mirrors and extends the VIAMA Surveillance Portal.
It has **no database of its own** — every byte comes from the VIAMA API over
HTTP. Build **all pages listed in §9**. Display **all data**. Nothing is
optional.

### Non-negotiable rules

1. **Never call the API from browser JavaScript.** The token is a permanent
   credential; putting it in front-end code leaks it to every visitor. All calls
   go through your own server (Next.js route handlers / Express / Django view /
   whatever), which holds the token in an environment variable.
2. **Always display `*_utc` timestamps**, converted to the viewer's locale — or
   to IST, since this is an Indian road-survey operation. Never display `*_raw`.
   See §4, it is the single easiest thing to get wrong.
3. **Paginate everything.** Lists cap at 500 rows/page. Follow `links.next`.
4. **Handle 409 properly.** It means "the workflow does not allow that right
   now" and the message says why. Show it to the user; do not retry.
5. **Cache read-only reference data** (`/meta/*`) for ~1 hour. Do not re-fetch
   the team map on every page render.

### Suggested stack

Anything server-rendered. Next.js (App Router) + TypeScript + Tailwind +
TanStack Query is a good default; so is Django + HTMX. The API is plain REST +
JSON, so nothing here depends on your choice.

---

## 1. CONNECTION

```
BASE URL   https://<viama-portal-host>/api/v1
AUTH       Authorization: Bearer <VIAMA_API_TOKEN>
```

Store as environment variables on the server:

```bash
VIAMA_API_URL=https://<portal-host>/api/v1
VIAMA_API_TOKEN=<the token you were given>
```

> If the portal was configured with `API_BASE_PATH`, the prefix will not be
> `/api/v1`. Use whatever prefix you were given — every path below is relative
> to it.
>
> If it was configured with `API_STEALTH=1`, a **404 may actually mean 401/403**
> (bad or missing token). Check your token before assuming a route is wrong.

**First call — confirm you are connected:**

```bash
curl -H "Authorization: Bearer $VIAMA_API_TOKEN" $VIAMA_API_URL/auth/whoami
```

Returns your `sub`, `scopes`, and `jti`. If this fails, nothing else will work.

### Minimal API client (adapt to your stack)

```ts
// server-side only
const BASE = process.env.VIAMA_API_URL!;
const TOKEN = process.env.VIAMA_API_TOKEN!;

export async function api<T>(
  path: string,
  init: RequestInit & { params?: Record<string, any> } = {}
): Promise<{ data: T; meta: any; links?: any }> {
  const url = new URL(BASE + path);
  for (const [k, v] of Object.entries(init.params ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }

  const res = await fetch(url, {
    ...init,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      ...init.headers,
    },
  });

  const body = await res.json().catch(() => null);

  if (!res.ok) {
    const err = body?.error ?? {};
    // err.code is stable and machine-readable; err.message is human-readable;
    // err.details lists the offending fields.
    throw Object.assign(new Error(err.message ?? res.statusText), {
      status: res.status,
      code: err.code,
      details: err.details,
      requestId: err.request_id,
    });
  }
  return body;
}
```

---

## 2. RESPONSE ENVELOPE

Every successful response:

```json
{
  "data": {} ,
  "meta": {
    "request_id": "0f3a9c2e…",
    "generated_at_utc": "2026-07-28T11:06:35Z",
    "generated_at_ist": "2026-07-28T16:36:35+05:30",
    "pagination": {
      "page": 1, "per_page": 50, "total": 812, "total_pages": 17,
      "has_next": true, "has_prev": false
    },
    "filters_applied": { "status": "completed" },
    "sort": "-start_time"
  },
  "links": { "self": "…", "next": "…", "prev": "…", "first": "…", "last": "…" }
}
```

`pagination` and `links` appear only on list responses.
`data` is an object for single resources, an array for lists.

---

## 3. ERRORS

```json
{
  "error": {
    "code": "invalid_state_transition",
    "message": "Cannot move a survey from 'completed' to 'video_pending'.",
    "status": 409,
    "details": [{ "field": "status", "issue": "illegal transition", "value": "video_pending" }],
    "request_id": "0f3a…"
  }
}
```

| Status | Codes | What your UI should do |
|---|---|---|
| 401 | `missing_token`, `invalid_token`, `token_revoked`, `token_version_stale` | Fatal config error. Alert an operator. |
| 403 | `insufficient_scope`, `state_not_authorized` | Your token lacks a scope. Fatal config error. |
| 404 | `not_found` | Show an empty state. |
| 409 | `conflict`, `survey_already_started`, `invalid_state_transition`, `already_reviewed`, `task_already_complete`, `captain_already_surveying` | **Show `message` to the user.** This is a business rule, not a bug. |
| 413 | `payload_too_large` | Upload > 25 MB. |
| 422 | `validation_error`, `idempotency_conflict` | Show `details` next to the offending form fields. |
| 429 | `rate_limited` | Back off, respect `Retry-After`. |
| 503 | `media_backend_unavailable`, `database_unavailable`, `api_disabled`, `schema_out_of_date` | Show a maintenance banner. |

**Unknown query parameters return 422.** A typo like `?statuss=completed` is
rejected rather than silently returning the whole unfiltered table. If you need
lenient behaviour, pass `?strict_params=false`.

---

## 4. TIMESTAMPS — READ THIS TWICE

The source database is internally inconsistent, so **every timestamp is returned
three ways**:

```json
{
  "start_time_utc": "2026-07-28T04:30:00Z",
  "start_time_ist": "2026-07-28T10:00:00+05:30",
  "start_time_raw": "2026-07-28T10:00:00"
}
```

| Suffix | Meaning | Use it? |
|---|---|---|
| `_utc` | The true instant, UTC | **YES — always use this** |
| `_ist` | Same instant, +05:30 | Fine, convenience |
| `_raw` | The naive value exactly as stored | Debugging only — **never display** |

**Why:** `surveys.start_time` and `end_time` store **IST wall-clock**. Every
other timestamp stores **UTC**. Reading `_raw` will be right for half the columns
and 5½ hours wrong for the other half. `GET /meta/version` documents the
semantics per column.

### The `display_*` trap

Dashboard endpoints also return `display_start_time` and `display_end_time`.
These reproduce **what the original portal shows on screen**, which is `raw +
5:30` applied to a column that is already IST — i.e. **5 hours 30 minutes ahead
of reality**. It is a bug in the original portal, preserved deliberately so your
screens can match theirs.

**Rule:** use `display_*` only on a page whose stated purpose is "look identical
to the old portal". Everywhere else use `*_utc`. If in doubt, use `*_utc`.

---

## 5. QUERYING

Applies to `/surveys`, `/assignments`, `/users`, `/audit/*` and all dashboards.

### Filter operators

```
?status=completed                    equals
?state__in=ODISHA,BIHAR              IN
?cycle_no__gte=2                     >=   (also __gt __lt __lte __ne)
?stretch_code__like=WB               ILIKE %WB%
?section_no__startswith=SEC          ILIKE SEC%
?end_survey_pdf__isnull=false        IS NOT NULL
?q=nitin                             search across the resource's text columns
```

### Survey-specific compound filters

| Param | Effect |
|---|---|
| `week=6` | Project week 6 (weeks start 2026-06-22) |
| `from_date=2026-07-01` / `to_date=2026-07-31` | Date range; `to_date` inclusive |
| `survey_date=2026-07-28` | One single day |
| `team=Krish\|Godbole\|Aspizo` | Expands to that team's states |
| `stretch=WB` | Partial match on stretch code |
| `has_pdf=true`, `has_dashcam_photo=true` | Media presence |
| `has_pending_task=true` | Any of the 3 team-leader tasks outstanding |

### Sorting

```
?sort=-start_time,section_no      leading '-' = descending, multi-key
```

Named presets that reproduce each original dashboard's exact row order —
**use these on the mirror dashboards**:

`admin_rank` · `regional_rank` · `teamleader_rank` · `roadvision_rank` · `day_order`

### Pagination

```
?page=2&per_page=100        max 500 (dashboards default 200)
?cursor=<opaque>            keyset mode — use for bulk sync, never skips rows
?count_total=false          skip the COUNT for speed
```

### Output shaping

```
?fields=id,section_no,status   sparse fieldsets
?derived=true                  attach the computed block
?time_format=iso               collapse to one key per timestamp (the UTC value)
?include_deleted=true          include soft-deleted records
```

---

## 6. THE DATA MODEL

### 6.1 Survey — the central entity

`GET /surveys/{id}` returns exactly these fields:

**Identity & location**
`id` · `captain_email` · `captain_name` · `section_no` · `upc_code` ·
`stretch_code` · `nh_number` · `ro` · `piu` · `state` · `section_length` (km)

**Lifecycle**
`status` · `survey_day` · `survey_type` · `cycle_no` · `is_resurvey` ·
`start_time_{utc,ist,raw}` · `end_time_{utc,ist,raw}`

**Media**
`dashcam_photo` · `end_survey_pdf` · `end_survey_photo` *(legacy — always empty,
never written by any route)*

**PDF re-upload**
`pdf_reupload_required` · `pdf_reupload_reason` · `pdf_reupload_count`

**Video upload**
`video_uploaded` · `video_pending_start_time_{utc,ist,raw}` ·
`video_upload_time_{utc,ist,raw}`

**Defect counts (8)**
`ir_lhs_count` · `mcw_lhs_count` · `service_lhs_count` · `slip_lhs_count` ·
`ir_rhs_count` · `mcw_rhs_count` · `service_rhs_count` · `slip_rhs_count`

**Automated bucket check**
`video_count_matched` (bool/null) · `video_count_checked_at_{utc,ist,raw}`

**Re-survey**
`resurvey_requested` · `resurvey_approved`

**Team-leader tasks** *(note the label mismatch — see §6.5)*
`survey_form_completed` + `survey_form_completed_at_{utc,ist,raw}`
`task1_completed` + `task1_completed_at_{utc,ist,raw}`
`task2_completed` + `task2_completed_at_{utc,ist,raw}`

**RoadVision review**
`roadvision_completed` · `roadvision_remark` ·
`roadvision_completed_at_{utc,ist,raw}`

**Visibility**
`show_on_dashboard` · `show_in_teamleader_dashboard`

**Computed by the API (not columns)**
`team` · `team_display` · `week_no` · `survey_ref_id`

### 6.2 Survey status machine

```
ongoing ──▶ groundwork_completed ──▶ video_pending ──▶ completed
   │                 │
   └────────┬────────┘
            ▼
        cancelled            (terminal)
```

**Forward only.** `status` is **not** settable via PATCH — it moves only through
the action endpoints in §7.2. Call
`GET /surveys/{id}/allowed-transitions` to know what a survey can do next; it
returns the legal next states, the endpoint for each, and which side-actions are
currently available. Drive your buttons from this.

Display labels: `ongoing` → "Ongoing", `groundwork_completed` → "Groundwork
Completed", `video_pending` → "Video Pending", `completed` → "Completed",
`cancelled` → "Cancelled".

### 6.3 SurveyAssignment (the weekly roster)

`id` · `captain_email` · `main_person` · `section_no` · `upc_code` ·
`stretch_code` · `state` · `nh_number` · `ro` · `piu` · `section_length` ·
`survey_day` · `survey_type` · `status` · `survey_enabled` ·
`dashcam_code` · `powerbank_code` · `missed_reason` · `missed_alert` ·
`alert_generated` · `alert_acknowledged` · `deadline_time_{utc,ist,raw}` ·
`last_week_reset` · `team`

Assignment status: `assigned` · `started` · `missed` · `completed` ·
`backup_in_progress` · `completed_by_backup`

### 6.4 User

`id` · `username` · `name` · `email` · `role` · `region` · `state`

`password_hash` is **never returned** — it is on an unconditional denylist and
cannot be requested even via `?fields=`.

Roles: `admin` · `captain` · `backup_captain` · `regional_manager` ·
`team_leader` · `roadvision`

### 6.5 Naming traps — preserve these exactly

| Field | Label shown to users |
|---|---|
| `task1_completed` | **"Raw Video"** |
| `task2_completed` | **"Final Report"** |
| `survey_form_completed` | "Survey Form" |
| team key `Godbole` | **"Viama"** |

Fetch these from `GET /meta/tasks` and `GET /meta/teams` rather than hardcoding.

### 6.6 Teams → states

| Key | Display | States |
|---|---|---|
| `Krish` | Krish | WEST BENGAL, ASSAM, BIHAR |
| `Godbole` | **Viama** | ODISHA |
| `Aspizo` | Aspizo | UTTAR PRADESH, JHARKHAND |

### 6.7 Derived values (`GET /surveys/{id}/derived`)

`survey_duration_minutes` · `upload_duration_minutes` · `upload_status_text` ·
`upload_is_overdue` · `defect_counts` · `scheduled_day` · `week_no` · `team` ·
`team_display` · `survey_ref_id` · `display_start_time` · `display_end_time`

`upload_is_overdue` is true past **480 minutes** — render that row red/bold, as
the original does.

---

## 7. ENDPOINT REFERENCE — ALL 182

### 7.1 Records (full CRUD)

| Method | Path |
|---|---|
| GET/POST | `/surveys` |
| GET/PATCH/PUT/DELETE | `/surveys/{id}` |
| POST | `/surveys/{id}/restore` |
| GET | `/surveys/count` |
| GET | `/surveys/statuses` — count per status over the filtered set |
| GET | `/surveys/stats` — `?group_by=state,status,team,week,cycle_no,captain_email,survey_day&metric=count\|km\|hours` |
| GET | `/surveys/{id}/derived` · `/timeline` · `/media` · `/defect-counts` · `/allowed-transitions` |
| GET | `/surveys/resurvey/pending` |
| GET/POST | `/users` |
| GET/PATCH/PUT/DELETE | `/users/{id}` |
| POST | `/users/{id}/restore` · `/users/{id}/password` |
| GET | `/users/by-email/{email}` · `/users/{id}/surveys` · `/users/{id}/assignments` · `/users/{id}/logins` |
| GET/POST | `/assignments` |
| GET/PATCH/PUT/DELETE | `/assignments/{id}` |
| POST | `/assignments/{id}/restore` · `/enable` · `/disable` |
| POST | `/assignments/bulk` — upsert many, keyed on `section_no` or `id` |
| GET | `/assignments/schedule-summary` — Mon–Fri counts |
| POST | `/assignments/weekly-reset` |
| GET/POST | `/schedules` (legacy table) |
| GET/PATCH/PUT/DELETE | `/schedules/{id}` |
| GET | `/schedules/by-captain/{email}` |
| GET/POST | `/equipment` |
| GET/PATCH/PUT/DELETE | `/equipment/{id}` |
| GET | `/equipment/types` |
| GET/POST | `/regional-manager-states` |
| GET/PATCH/DELETE | `/regional-manager-states/{id}` |
| GET/PUT | `/regional-manager-states/by-manager/{email}` |

**DELETE is a soft delete** — the record is tombstoned and hidden, not removed.
`POST /{id}/restore` undoes it. `?hard=true` needs the `admin:destroy` scope and
is refused if other rows still reference the record.

### 7.2 Workflow actions (all POST)

| Path | Body | Effect |
|---|---|---|
| `/surveys/start` | `{assignment_id*, captain_email, survey_type, survey_day, dashcam_photo, is_resurvey, force}` | Creates the survey. 409 `survey_already_started` if this section was already surveyed this week. **Send an `Idempotency-Key` header.** |
| `/surveys/{id}/groundwork-complete` | `{}` | → `groundwork_completed` |
| `/surveys/{id}/complete` | `{pdf_url}` or multipart `survey_pdf` | → `video_pending` |
| `/surveys/{id}/video-counts` | all 8 count fields, ints ≥ 0 | → `completed` |
| `/surveys/{id}/cancel` | `{reason}` | → `cancelled` |
| `/surveys/{id}/resurvey/request` · `/approve` · `/reject` | `{}` | Re-survey workflow |
| `/surveys/{id}/pdf-reupload/request` | `{reason*}` | Flags the PDF for replacement |
| `/surveys/{id}/pdf-reupload` | `{pdf_url}` or multipart | Supplies the replacement |
| `/surveys/{id}/roadvision-review` | `{remark*}` | One-shot; 409 if already reviewed |
| `/surveys/{id}/tasks/{task1\|task2\|survey_form}` | `{completed: true}` | One-shot; 409 if already done |
| `/surveys/{id}/visibility` | `{show_on_dashboard, show_in_teamleader_dashboard}` | |
| `/assignments/{id}/unable-to-survey` | `{reason*}` | Records why |
| `/assignments/{id}/acknowledge-alert` | `{scope: "captain_day"\|"self"}` | Dismisses a missed alert |

### 7.3 Dashboards — the exact payload each original page renders

| Path | Notes |
|---|---|
| `/dashboards/admin` | KPIs, team KMs, alerts, resurvey requests, dropdowns, rows |
| `/dashboards/admin/missed` · `/schedules` · `/reports` | |
| `/dashboards/admin/surveys/{id}` · `/remark/{id}` | |
| `/dashboards/admin/gmail-drafts` (GET) | Week + section pickers |
| `/dashboards/admin/gmail-drafts/{defect\|raw}` (POST) | `{survey_id*, start_date, end_date}` → subject + HTML body |
| `/dashboards/regional?manager_email=…` | State-scoped; 403 outside scope |
| `/dashboards/regional/missed` · `/schedules` · `/surveys/{id}` | |
| `/dashboards/teamleader` · `/schedules` · `/surveys/{id}` | |
| `/dashboards/roadvision` · `/surveys/{id}` · `/remark/{id}` | |
| `/dashboards/captain/home?captain_email=…` | |
| `/dashboards/captain/assignments` · `/checklist` · `/recording` | |
| `/dashboards/captain/pending-uploads` · `/completed-surveys` | |
| `/dashboards/captain/video-counts/{id}` · `/unable-to-survey` | |
| `/dashboards/backup/home` · `/pending-uploads` · `/completed-surveys` | |

`/dashboards/admin` returns:
`total_captains` · `ongoing` · `groundwork_completed` · `completed` ·
`video_pending` · `missed` · `krish_km` · `godbole_km` · `aspizo_km` ·
`total_km` · `all_surveys[]` · `resurvey_requests[]` · `alerts[]` · `states[]` ·
`captains[]` · `cycles[]` · `weeks[]` · `pagination`

Each row in `all_surveys[]` is a full Survey **plus** `scheduled_day`,
`display_start_time`, `display_end_time`, `upload_duration_minutes`,
`upload_status_text`, `survey_duration_minutes`.

> Two quirks faithfully preserved: the regional dashboard returns `alerts: []`
> and `resurvey_requests: []`, and the team-leader dashboard returns `missed: 0`
> — because the original pages hardcode them. Pass **`?live=true`** to compute
> the real values instead. On the mirror site, **use `?live=true`** — you want
> real numbers.

### 7.4 Reports & exports

`/reports/summary` · `/reports/team-km` · `/reports/weekly?from_week=&to_week=` ·
`/reports/captain-performance` · `/reports/upload-sla?threshold=480`

`/exports/surveys.xlsx` (byte-compatible with the original portal's export) ·
`/exports/surveys.csv` · `/exports/assignments.xlsx` · `/exports/columns`

Exports return binary — **stream them through**, do not buffer:

```ts
const upstream = await fetch(`${BASE}/exports/surveys.xlsx?${qs}`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
});
return new Response(upstream.body, {
  headers: {
    "Content-Type": upstream.headers.get("Content-Type")!,
    "Content-Disposition": 'attachment; filename="Survey_Report.xlsx"',
  },
});
```

### 7.5 Alerts

`GET /alerts` — **read-only, never writes.** Returns `alerts[]`, `changes[]`
(what a write run *would* do), `missed_total`, `committed: false`.
`GET /alerts/missed` · `GET /alerts/summary` · `POST /alerts/evaluate` (writes)

Alert objects: `assignment_id` · `captain` · `message` · `state` · `stretch` ·
`section_no` · `reason`

### 7.6 Audit / access logs

| Path | Returns |
|---|---|
| `/audit/logins` | Every login, failed login and logout |
| `/audit/logins/{id}` | One event |
| `/audit/failed-logins?days=7` | Grouped by identifier and by IP |
| `/audit/requests` | Full access log |
| `/audit/requests/{id}` | One request |
| `/audit/sessions?days=30` | Logins rolled up, with request counts |
| `/audit/sessions/{session_id}` | Everything on one session, in order |
| `/audit/active-users?days=30` | Who has been using the portal |
| `/audit/ips?days=30` | Grouped by source IP |
| `/audit/summary?days=7` | Rollup for a dashboard |
| `/audit/config` | What is captured, retention window |

**LoginEvent fields:** `id` · `event_type` · `user_id` · `username` · `email` ·
`name` · `role` · `login_identifier` · `failure_reason` · `ip` ·
`forwarded_for` · `user_agent` · `device_type` · `browser` · `platform` ·
`country` · `region` · `city` · `timezone` · `method` · `path` · `referer` ·
`host` · `protocol` · `session_id` · `request_id` · `created_at_{utc,ist,raw}`

`event_type` ∈ `login_success` · `login_failed` · `logout` · `api_token_used`

**RequestLog fields:** `id` · `method` · `path` · `query_string` · `endpoint` ·
`status_code` · `duration_ms` · `channel` · `user_id` · `email` · `role` ·
`token_sub` · `session_id` · `ip` · `forwarded_for` · `user_agent` ·
`device_type` · `country` · `city` · `referer` · `content_length` ·
`response_bytes` · `request_id` · `error_code` · `created_at_{utc,ist,raw}`

Requires the **`audit:read`** scope, which is deliberately excluded from the
read-only bundle. If you get 403 here, your token was not granted it.

> **These logs contain IP addresses — personal data.** Put the audit pages
> behind your strictest access control, and do not export them casually.

### 7.7 Database dump

`/dump/manifest` · `/dump/tables` · `/dump/status` ·
`/dump` (whole DB, NDJSON) · `/dump/{table}?format=ndjson|json|csv|sql` ·
`/dump/{table}/checksum`

Add `?compress=gzip`. Incremental: `?since_map={"surveys":812}` on the full
dump, `?since=<id>` per table. Resume an interrupted dump with `?after_id=<id>`.

Full-dump structure: a `_meta` line, one line per row tagged `_table`, then a
`_summary` line. **The `_summary` line is the proof the download completed** — a
truncated file will not have it. Verify it.

A ready-made daily client already exists: `core/dump_client.py` in the portal
repo. Copy it to your VM, no dependencies beyond the stdlib.

### 7.8 Sync / webhooks

`/sync/changes?since=<cursor>` · `/sync/cursor` · `/sync/status` ·
`/sync/entities`
`/webhooks/endpoints` (GET/POST) · `/{id}` (GET/PATCH/DELETE) · `/{id}/test` ·
`/webhooks/events` · `/webhooks/outbox` · `/webhooks/deliveries` ·
`/webhooks/outbox/{id}/retry` · `/webhooks/drain` · `/webhooks/health`

### 7.9 Meta / reference

`/meta/health` *(no auth)* · `/meta/integrity` *(no auth)* · `/meta/version` ·
`/meta/weeks` · `/meta/weeks/current` · `/meta/teams` · `/meta/states` ·
`/meta/cycles` · `/meta/statuses` · `/meta/roles` · `/meta/days` ·
`/meta/survey-types` · `/meta/checklists` · `/meta/defect-fields` ·
`/meta/tasks` · `/meta/email-types` · `/meta/filters?scope=…` ·
`/meta/endpoints` · `/openapi.json`

`/auth/whoami` · `/auth/scopes` · `/auth/verify`

### 7.10 Media

`POST /media/images` · `POST /media/pdfs` · `POST /media/pdfs/supabase` ·
`GET /media/status` · `PUT|DELETE /surveys/{id}/media/{kind}`

Multipart field name is `file`. Max 25 MB. A 503 `media_backend_unavailable`
means the portal's Google Drive credential expired — surface it as such, it is
not your bug.

---

## 8. LIVE UPDATES

Two channels. **Implement both.**

### Webhooks (fast path)

Register your receiver:

```bash
curl -X POST "$VIAMA_API_URL/webhooks/endpoints" \
  -H "Authorization: Bearer $VIAMA_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://your-site/hooks/viama","events":"*"}'
```

The response contains a `secret` **shown once** — store it.

**Verify every delivery.** Header:

```
X-Viama-Signature: t=<unix>,v1=<hex hmac_sha256(secret, "<t>.<raw body>")>
```

```ts
import crypto from "crypto";

export function verify(secret: string, header: string, rawBody: string): boolean {
  const parts = Object.fromEntries(header.split(",").map(p => p.split("=", 2)));
  const t = Number(parts.t);
  if (Math.abs(Date.now() / 1000 - t) > 300) return false;   // replay window
  const expected = crypto.createHmac("sha256", secret)
                         .update(`${t}.${rawBody}`).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}
```

**You must hash the raw body**, before any JSON parsing/re-serialisation.
**Run NTP on your server** — clock skew is the most common cause of failures.

Payload:

```json
{
  "id": "d7c1…",
  "event": "survey.status_changed",
  "occurred_at_utc": "2026-07-28T09:15:03Z",
  "api_version": "v1",
  "source": "api" | "portal",
  "actor": { "type": "session", "user_id": 4, "role": "team_leader" },
  "data": {
    "object": "survey",
    "id": 812,
    "op": "update",
    "changed": { "status": { "from": "ongoing", "to": "video_pending" } },
    "snapshot": { }
  }
}
```

Events: `survey.created` · `survey.status_changed` · `survey.groundwork_submitted`
· `survey.completed` · `survey.cancelled` · `survey.deleted` ·
`survey.video_counts_submitted` · `survey.video_count_checked` ·
`survey.resurvey_requested` · `survey.resurvey_approved` ·
`survey.pdf_reupload_requested` · `survey.pdf_reuploaded` ·
`survey.roadvision_reviewed` · `survey.task_toggled` · `survey.media_updated` ·
`assignment.created|updated|missed|alert_acknowledged|bulk_changed|deleted` ·
`user.*` · `schedule.*` · `equipment.*` · `manager_state.*` ·
`auth.login_success` · `auth.login_failed` · `auth.logout`

Full catalog with descriptions: `GET /webhooks/events`.

Return **2xx quickly** — queue the work, don't process inline. Non-2xx triggers
retry with backoff (1m, 5m, 30m, 2h, 6h, 24h).

### Change feed (reliable path)

Webhooks can be missed. Poll this to guarantee completeness:

```
GET /sync/cursor                      → starting cursor
GET /sync/changes?since=<cursor>      → { changes[], next_cursor, has_more }
```

Store `next_cursor`, pass it back next time, loop while `has_more`.

The feed is intentionally ~5 seconds behind: a database sequence value is
allocated at INSERT but only becomes visible at COMMIT, so without that lag a
poller would permanently skip rows that committed out of order. Do not try to
"fix" it.

**Recommended:** webhooks for immediacy + a `/sync/changes` poll every 60s as a
safety net + a nightly `/dump` reconciliation.

---

## 9. PAGES TO BUILD

Build all of these.

### A. Shell

- **A1 Layout** — sidebar nav, section groups, global search box, connection
  status pill (green/red, driven by `/meta/health` + `/meta/integrity`), last-sync
  timestamp.
- **A2 Login** — your own auth. **Do not reuse VIAMA credentials.** The API token
  is a server secret; your site's users are your own.
- **A3 Global search** — one box querying `/surveys?q=`, `/assignments?q=`,
  `/users?q=` in parallel, grouped results.

### B. Overview

- **B1 Home / Command Centre**
  - KPI tiles: Captains, Ongoing, Groundwork, Video Pending, Completed, Missed
    (`/dashboards/admin`)
  - Team KM tiles: Krish, **Viama** (key `Godbole`), Aspizo, Total
  - Live alerts strip (`/alerts`)
  - Pending re-survey requests (`/surveys/resurvey/pending`)
  - Weekly trend chart (`/reports/weekly`)
  - Status donut (`/surveys/statuses`)
  - Recent activity feed (`/sync/changes`)

### C. Surveys

- **C1 Survey list** — full-width table, all filters from §5, the four sort
  presets, column chooser, CSV/XLSX export buttons, saved views, pagination.
  Columns: UPC, Section, Cycle (`N (R)` if `is_resurvey`), Scheduled Day, Start,
  End, Captain, Status badge, Survey Duration, Upload Duration (red past 480 min),
  Survey Form, Raw Video, Final Report, RoadVision, Actions.
- **C2 Survey detail** — header (UPC, section, status, team), all §6.1 fields
  grouped, dashcam photo, PDF link, the 8 defect counts as an LHS/RHS grid,
  `video_count_matched` badge, RoadVision remark, PDF re-upload state, derived
  metrics, and an **action bar driven by `/surveys/{id}/allowed-transitions`**.
- **C3 Survey timeline** — vertical timeline from `/surveys/{id}/timeline`.
- **C4 Survey map/board** *(optional)* — kanban by status, grouped by team.
- **C5 Create/Edit survey** — form; `status` is read-only, actions move it.

### D. Assignments & schedule

- **D1 Assignment list** — filters, day/state, enabled toggle, bulk actions.
- **D2 Assignment detail/edit** — plus "Unable to survey" and "Acknowledge alert".
- **D3 Weekly schedule board** — Mon–Fri columns with counts from
  `/assignments/schedule-summary`, cards per assignment, state filter.
- **D4 Bulk import** — CSV upload → preview → `POST /assignments/bulk`, showing
  created / updated / skipped / per-row errors.
- **D5 Missed surveys** — `/alerts/missed`, with acknowledge buttons.

### E. Role dashboards (mirror the originals)

- **E1 Admin dashboard** — `/dashboards/admin` (already returns real alerts;
  `?live=true` is only needed on regional and team-leader)
- **E2 Regional dashboard** — manager picker → `/dashboards/regional?manager_email=…&live=true`
- **E3 Team-leader dashboard** — `/dashboards/teamleader?live=true`, with the
  three task toggles calling `/surveys/{id}/tasks/{task}`
- **E4 RoadVision queue** — `/dashboards/roadvision`, unreviewed first, review
  modal posting `{remark}`
- **E5 Captain view** — captain picker → home, assignments, pending uploads,
  completed surveys, recording state
- **E6 Backup captain view** — `/dashboards/backup/*`

### F. Reports & analytics

- **F1 Reports** — the four stat cards + filtered table + export.
- **F2 Team performance** — `/reports/team-km`, bar chart + table.
- **F3 Weekly trends** — `/reports/weekly`, multi-series line chart.
- **F4 Captain performance** — `/reports/captain-performance`, sortable table.
- **F5 Upload SLA** — `/reports/upload-sla`; p50/p90/p99, breach list, histogram.
- **F6 Custom analytics** — a UI over `/surveys/stats` (pick `group_by` +
  `metric`, render chart + table).

### G. People

- **G1 User list** — role/region/state filters.
- **G2 User detail** — profile, their surveys, their assignments, their login
  history (`/users/{id}/logins`).
- **G3 Create/edit user**, **G4 Password reset**.
- **G5 Regional manager scoping** — manage state sets via
  `/regional-manager-states/by-manager/{email}`.

### H. Security & audit

- **H1 Audit overview** — `/audit/summary`, logins over time, by role, by device,
  error-rate chart.
- **H2 Login history** — `/audit/logins`, full table, all filters, CSV export.
- **H3 Failed logins / threat view** — `/audit/failed-logins`, grouped by
  identifier and IP, highlight one IP hitting many accounts.
- **H4 Sessions** — `/audit/sessions`, expandable to `/audit/sessions/{id}`.
- **H5 Access log** — `/audit/requests`, filter by path/status/channel/user.
- **H6 Active users** — `/audit/active-users`.
- **H7 IP explorer** — `/audit/ips`.

### I. Reference

- **I1 Checklists** — the 20 pre-survey + 7 recording items (`/meta/checklists`).
- **I2 Reference data** — teams, states, statuses, roles, days, survey types,
  defect fields, weeks.
- **I3 Equipment**, **I4 Legacy schedules**.

### J. Operations

- **J1 Integration health** — `/meta/health`, `/meta/integrity`,
  `/webhooks/health`, `/sync/status`. **Alarm when `/meta/integrity` reports
  `ok: false`** — that means a core API file was deleted on the portal.
- **J2 Webhook manager** — endpoints CRUD, test-ping, delivery history, retry.
- **J3 Sync monitor** — cursor position, lag, manual catch-up.
- **J4 Backup manager** — dump manifest, sizes, run history, checksums,
  download links.
- **J5 Email drafts** — mirror of the Gmail-draft builder: week picker → section
  picker → dates → `POST /dashboards/admin/gmail-drafts/{type}` → rendered
  subject + HTML with copy buttons.

### K. Live status

- **K1 Live operations board** — auto-refreshing view of in-flight surveys, with
  elapsed timers ticking client-side from `start_time_utc`, red past 480 min.

---

## 10. UI CONVENTIONS

**Status badges**

| Status | Suggested colour |
|---|---|
| `ongoing` | blue |
| `groundwork_completed` | indigo |
| `video_pending` | amber |
| `completed` | green |
| `cancelled` | grey |
| assignment `missed` | red |

Add a distinct pill when `pdf_reupload_required` is true.

**Durations** — render as `Xh Ym`. Upload duration turns red/bold above 480
minutes (`upload_is_overdue`).

**Live timers** — for in-flight surveys, tick client-side from `start_time_utc`;
do not poll the API every second.

**Empty states** — every table needs one. Many filters legitimately return zero.

**Loading** — skeletons, not spinners, for tables.

**Accessibility** — status must not be conveyed by colour alone; include text.
Tables need proper headers and scope attributes.

---

## 11. IMPLEMENTATION ORDER

1. API client + auth + error handling (§1–§3). Verify with `/auth/whoami`.
2. Shell, nav, health pill.
3. Survey list + detail — the core of the product.
4. Admin dashboard.
5. Assignments + schedule board.
6. The other role dashboards.
7. Reports + analytics + exports.
8. Users + regional scoping.
9. Audit pages.
10. Webhook receiver + sync poller.
11. Ops pages, backup manager, email drafts.
12. Polish: saved views, column chooser, dark mode, mobile.

---

## 12. TESTING CHECKLIST

- [ ] `/auth/whoami` succeeds; a bad token surfaces a clear operator error
- [ ] Every list paginates past page 1 and follows `links.next`
- [ ] Filters compose (status + week + team + date range together)
- [ ] All four sort presets produce the documented ordering
- [ ] Timestamps display in IST and match the portal (accounting for the
      `display_*` offset described in §4)
- [ ] 409 from a workflow action shows its message to the user, no retry
- [ ] 422 highlights the offending field from `details`
- [ ] Exports download with correct filename and content type
- [ ] Webhook signature verification rejects a tampered body and a wrong secret
- [ ] Replaying an old webhook (t older than 300s) is rejected
- [ ] Sync cursor advances and survives a restart
- [ ] `/meta/integrity` returning `ok:false` raises a visible alarm
- [ ] Audit pages 403 cleanly if the token lacks `audit:read`
- [ ] The API token never appears in any client-side bundle or network response

---

## 13. KNOWN LIMITS — DESIGN AROUND THESE

1. **Two portal code paths bypass change capture.** The Monday assignment reset
   and bulk alert-acknowledge use raw SQL, so they emit **no webhook and no
   change-log entry**. Reconcile assignments on a schedule (nightly full pull)
   rather than trusting the event stream alone.
2. **The source portal mutates data when its pages are viewed.** `GET /admin` and
   `GET /regional` run the missed-survey engine and commit. The API never does.
   So the "missed" count can differ between the two systems until someone loads
   the original admin page — or until `POST /alerts/evaluate` runs.
3. **Soft deletes are not fully hidden in the source portal.** Records you delete
   via the API disappear from the API and from the original admin/regional/
   team-leader dashboards, but still appear on its RoadVision and captain pages.
4. **Timezone semantics were inferred** from how each column is written. Before
   trusting historical `*_utc` values, verify one survey whose real start time is
   known.
5. **`end_survey_photo` is always empty.** It is a legacy column no code writes.
   Do not build UI that depends on it.
6. **`equipment` is effectively unused** by the source portal — dashcam and
   powerbank codes live denormalised on assignment rows.
7. **Lists cap at 500 rows.** The original pages render everything unpaginated,
   so a naive 1:1 port will silently truncate. Always paginate.
8. **Media endpoints return 503** when the portal's Google Drive token expires.
   Show it as an upstream outage, not a failure of your site.
9. **`section_no` is not guaranteed unique** in the assignments table. Where the
   API resolves an assignment from a survey (to derive `scheduled_day`) it takes
   the lowest matching id. That is deterministic, but if your data has duplicate
   `section_no` values it may not be the row a human would have picked.
10. **`?expand=` is accepted but does nothing.** It is reserved for a future
   release; it will not error, but it will not embed related records either.
   Fetch related data with a second call (`/users/{id}/surveys`, etc.).

---

## 14. QUICK REFERENCE

```bash
export U="https://<portal>/api/v1"
export T="<token>"
alias vq='curl -sS -H "Authorization: Bearer $T"'

vq "$U/auth/whoami"
vq "$U/meta/health"
vq "$U/meta/endpoints"                              # every route, grouped
vq "$U/openapi.json"                                # machine-readable spec

vq "$U/surveys?status=completed&week=6&per_page=5"
vq "$U/surveys/1?derived=true"
vq "$U/surveys/stats?group_by=team&metric=km"
vq "$U/dashboards/admin"
vq "$U/reports/summary"
vq "$U/audit/logins?days=7"
vq "$U/sync/cursor"
vq "$U/dump/manifest"
vq "$U/dump?compress=gzip" -o dump.ndjson.gz
```

---

**Build every page in §9. Fetch every field in §6. Respect §4 and §13.**
