# Deployment — automated RoadVision video-count remarks

Everything below is what must happen **on Vercel** to make the automated remark
system run in production. The code, the database migration, and the first live
sync have already been done locally against the production database.

---

## 0. What already exists

| Thing | State |
|---|---|
| DB columns `video_count_matched`, `video_count_checked_at` | ✅ already added to production `surveys` |
| First sync over the 10-day window | ✅ already run (88 checked → 57 matched, 24 shortfall, 7 no-data) |
| `vercel.json` cron entry | ✅ committed, but **only fires once deployed** |
| `RV_DASHBOARD_TOKEN` / `CRON_SECRET` | ⚠️ **local `.env` only — must be added in Vercel** |

`.env` is **not** uploaded by Vercel. Without step 1 the cron will return
`503 upstream_unavailable` and write nothing.

---

## 1. Add the environment variables

**Vercel dashboard → your project → Settings → Environment Variables.**

Add each of the following. Tick **Production**, **Preview**, and **Development**
for all of them.

| Name | Value | Notes |
|---|---|---|
| `RV_DASHBOARD_TOKEN` | *(copy from local `.env`)* | RoadVision API bearer token. **Expires 2027-07-28.** The sync warns in the log and in its summary from 30 days out. |
| `CRON_SECRET` | *(copy from local `.env`)* | Guards `/cron/video-count-sync`. Vercel Cron also auto-sends this as `Authorization: Bearer`. |
| `DATABASE_URL` | *(copy from local `.env`)* | Only if not already set on the project. |
| `SECRET_KEY` | *(copy from local `.env`)* | Flask session key. Was hardcoded; now required. Without it the app falls back to the old public default and logs a warning. |
| `SUPABASE_ANON_KEY` | *(copy from local `.env`)* | Was hardcoded in `supabase_client.py`. Same fallback behaviour. |
| `API_JWT_SECRET` | *(copy from local `.env`)* | Only if you are using the JSON API. Without it every `/api/v1` call returns `503 api_misconfigured`. |
| `RV_RAW_XLSX_API_KEY` | *(RoadVision API key)* | Required to fetch the raw XLSX attachment for approved survey emails. |
| `RV_DEFECT_REPORT_API_KEY` | *(optional override)* | Defect-report API key; defaults to `RV_RAW_XLSX_API_KEY`. |
| `M365_TENANT_ID` | *(Entra tenant ID)* | Microsoft Graph app-only email authentication. |
| `M365_CLIENT_ID` | *(Entra application/client ID)* | Microsoft Graph app-only email authentication. |
| `M365_CLIENT_SECRET` | *(new secret value)* | Store only as an environment secret. The secret previously shared in chat must be revoked and replaced. |

`.env.example` lists every variable the app reads, including the optional ones.

The approved survey email uses Microsoft Graph to send as
`projects9@adorpower.com`. The Entra app needs Microsoft Graph **Application**
permission `Mail.Send` with admin consent, and Exchange must allow the app to
send as that mailbox. The raw XLSX API key and all three `M365_*` variables are
required locally and in Vercel. Never commit or share the client secret.

Before deploying defect-report email tracking, run
`python migrate_defect_report_email_delivery.py` once against the target database.
It adds nullable delivery markers without changing existing Gmail-derived
defect-report dates.

The exact values are in your local
`VIAMA-main/.env`. Copy them verbatim — no quotes, no trailing spaces.

### Or via CLI

```bash
vercel env add RV_DASHBOARD_TOKEN production
vercel env add CRON_SECRET production
# repeat for preview / development
```

> ⚠️ Environment variables are only picked up by **new** deployments. After adding
> them you must redeploy — editing a variable does not restart the existing build.

---

## 2. Deploy

```bash
python check_cron_schedules.py    # refuses anything Vercel would reject
python check_database_schema.py   # refuses a database missing the columns the code selects
vercel --prod
```

or push to the branch connected to the Vercel project.

> The guards are not optional in practice. A cron expression that fires more than
> once a day makes Vercel **drop the deployment silently** — no entry in the
> Deployments list, no error anywhere, and production keeps serving the
> previous build. See "The cron schedule breaks the deployment" below.
>
> A build can also succeed and still crash on the first request when the
> database is behind the code. See "The migration ran against the wrong
> database" below.

---

## 3. Confirm the cron registered

**Vercel dashboard → your project → Settings → Cron Jobs.**

You should see:

| Path | Schedule |
|---|---|
| `/cron/video-count-sync` | `0 2 * * *` |
| `/cron/approved-survey-emails` | `0 3 * * *` |

The video-count sync remains at **02:00 UTC = 07:30 IST**, daily. Approved
survey emails are retried at **03:00 UTC = 08:30 IST**, daily, with up to 10
emails per phase per run.

Only the **raw-video email** (survey form PDF + raw XLSX) goes out today. The
defect-report mailing is switched off — see "Defect-report mail is switched
off" below.

> **Every cron on this project must fire at most once per day.** Vercel Hobby
> rejects any expression that runs more often, and the rejection kills the
> deployment. See "The cron schedule breaks the deployment" below.

---

## Defect-report mail is switched off

The portal sends two different mails once a survey is approved:

| Mail | Contains | State |
|---|---|---|
| **Raw-video email** | the survey-form PDF + the raw-video XLSX from RoadVision | **on** |
| Defect-report email | the defect-report XLSX from RoadVision | **off** |

The defect-report mail is off because it is a separate decision from the
raw-video mail, and only the raw-video mail is wanted right now. It is
controlled by one environment variable, read at send time:

| Variable | Default | Meaning |
|---|---|---|
| `DEFECT_REPORT_EMAIL` | `0` (off) | `1` sends the defect-report XLSX as well |

The switch guards **both** places the defect mail could go out — the cron
phase and the send chained onto the raw-video email — so it cannot leak from
one while the other is off.

**To start the defect mail later:** set `DEFECT_REPORT_EMAIL=1` in the Vercel
dashboard and redeploy. No code change is needed.

**While it is off** the cron summary says so, so an empty run is not a bug:

```json
{"raw_video_emails": {"checked": 3, "sent": 3, "waiting_for_raw_xlsx": 23, "failed": 0},
 "defect_report_emails": {"skipped": "disabled via DEFECT_REPORT_EMAIL=0", "checked": 0, "sent": 0, "failed": 0}}
```

The surveys waiting for a defect mail are **not lost** — they stay
`defect_report_email_sent_at = NULL` and are picked up the moment the switch
is turned on. The admin dashboard's *Defect Report Email Sent* column shows
the same thing.

---

## 4. Smoke-test the deployed endpoint

Replace `<CRON_SECRET>` with the real value.

**Dry run first — writes nothing:**

```bash
curl -s "https://viama-three.vercel.app/cron/video-count-sync?dry_run=1" \
  -H "X-Cron-Secret: <CRON_SECRET>"
```

Expected — a JSON summary, e.g.:

```json
{"window_days":10,"considered":88,"skipped_already_matched":57,
 "skipped_manual_remark":0,"checked":31,"matched":0,
 "shortfall":24,"no_bucket_data":7,"dry_run":true}
```

**Then confirm auth actually rejects:**

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://viama-three.vercel.app/cron/video-count-sync"
```

Expected: `403`

**Live run (optional — the cron will do this anyway):**

```bash
curl -s "https://viama-three.vercel.app/cron/video-count-sync" \
  -H "X-Cron-Secret: <CRON_SECRET>"
```

---

## 5. Verify in the UI

1. Log in as **admin** → dashboard → the **RV Remark** column shows **View** links.
2. Open one → `/admin/remark/<id>` renders the generated text.
3. A passing survey reads exactly:
   *"The number of uploaded videos in the GCP bucket matches the enlisted video
   count, and all videos represent the complete surveyed length."*
4. A failing survey names the specific short cells and asks for a GCS re-check.

An admin can also re-run the sync on demand at any time by visiting
`/cron/video-count-sync` while logged in — no secret needed for a logged-in admin.

---

## Rescheduled surveys auto-cancel after Sunday 23:30 IST

A survey a captain pushed back stays **Rescheduled** so the team can see it is
coming. Once **Sunday 23:30 IST of the week it was rescheduled in** passes without
the survey being restarted, it becomes **Cancelled** automatically.

The cutoff is derived from the row's own week, not from "now": rescheduled on
Wednesday → that coming Sunday 23:30; rescheduled Sunday morning → 23:30 the same
day; already overdue from a previous week → cancelled on the next run.

The clock time is `RESCHEDULED_EXPIRY_HOUR` / `RESCHEDULED_EXPIRY_MINUTE` in
`core/config.py`, and `RESCHEDULED_EXPIRED_NOTE` is built from them, so the
"View Reason" text always matches the deadline. Changing the time is a two-line
edit there — nothing else hardcodes it.

23:30 is deliberately the last moment of the week before the Monday-00:00 rollover
that clears the assignment, so a captain gets the whole of Sunday rather than
losing the weekend at mid-afternoon. Nothing depends on that 30-minute margin:
the deadline comes from the row's own anchor, so a sweep that first runs on Monday
still finds last week's row overdue and cancels it. The only thing the late hour
costs is that a reschedule created in the final half hour of Sunday is worth very
little.

**Two triggers, both already deployed with the code — no infra change needed:**

| Trigger | What it covers |
|---|---|
| Dashboard load (`/admin`, `/regional`, `/form-approver`) | Correct the moment anybody looks |
| `POST /api/v1/jobs/tick` | Quiet days when nobody logs in |

There is deliberately **no in-process timer**. Vercel freezes the function the
moment the response is written, so a background thread fires minutes late or never,
and fails silently in production only.

The dashboard trigger is what makes this work on the current Hobby plan, whose one
daily cron slot is already used by `/cron/video-count-sync`. If you upgrade to Pro
and want the job to run without anyone opening a dashboard, point a second cron at
the tick endpoint — it runs every job, and `?only=` narrows it:

```json
{"path": "/api/v1/jobs/tick", "schedule": "0 9 * * *"}
```

`0 9 * * *` is 14:30 IST, which is after the 14:00 IST gate the `alerts` job
applies. **Do not** repoint the existing 02:00 UTC cron at `/jobs/tick` instead:
07:30 IST is before that gate, so the missed-survey engine would no-op every day.

**Dry run — writes nothing:**

```bash
curl -s -X POST \
  "https://viama-three.vercel.app/api/v1/jobs/tick?only=rescheduled_expiry&dry_run=true" \
  -H "X-Cron-Secret: <CRON_SECRET>"
```

`would_cancel` is how many rows the next real run would flip.

**Kill switch** — set `RESCHEDULED_EXPIRY=0` in Vercel and redeploy. The sweep
returns `{"ran": false, "skipped": "disabled via RESCHEDULED_EXPIRY=0"}` and
touches nothing, including the dashboard trigger. Set it back to `1` to resume.

> **The first run after this shipped was large.** The Monday reset clears
> `SurveyAssignment` but never touched `Survey.status`, so 116 rescheduled rows
> dating back to the week of 2026-07-05 were still sitting in `rescheduled`
> forever. They were cancelled on that first run, which is the intended
> behaviour, but it is why the admin dashboard's Rescheduled count dropped
> sharply. No rows were deleted — they remain visible under Cancelled. Automatic
> cancellation does not populate a reason.

---

## The migration ran against the wrong database

**Symptom:** the build is green, but every page that touches the changed model
returns 500 and the log says:

```
psycopg2.errors.UndefinedColumn: column surveys.defect_report_email_sent_at does not exist
```

**Cause:** `migrate_*.py` reads `DATABASE_URL` from `.env`, and there are two
Supabase projects in play that are **not** interchangeable:

| | Project | Role |
|---|---|---|
| Production | `nahcqrxmbzxuixqizdga` (ap-southeast-2) | The one Vercel connects to. The live database. |
| Report copy | `zgmzbezgyvivmtkizzvg` (ap-south-1) | A snapshot taken on 2026-10-17 for reporting. It is stale — 41 surveys behind production when last compared. |

The local `.env` points `DATABASE_URL` at the **report copy**, so a migration
run from this checkout lands there and production keeps the old schema. The
code is fine; the database is behind. This is exactly what happened on
2026-10-22: `migrate_defect_report_email_delivery.py` was run against the copy,
and production's `/admin` returned 500 until the same migration was applied to
production.

**Fix:** run the migration against production, then check before deploying.

```bash
# check what production is missing (reads PRODUCTION_DATABASE_URL)
python check_database_schema.py

# run a migration against production
DATABASE_URL="$PRODUCTION_DATABASE_URL" python migrate_defect_report_email_delivery.py
```

On Windows PowerShell:

```powershell
$env:DATABASE_URL = $env:PRODUCTION_DATABASE_URL
python migrate_defect_report_email_delivery.py
```

`check_database_schema.py` compares the schema the models expect against the
schema the database has, and exits non-zero listing every missing table and
column. It exits 0 only when the database is complete.

> Keep `PRODUCTION_DATABASE_URL` set in `.env`. It is the single place that
> names the live database, so a migration can never be aimed at the copy by
> accident.

---

## The cron schedule breaks the deployment

**Symptom:** you push, and production keeps serving the *old* build. Nothing
new appears in the Deployments list, nothing appears in the Activity log, and a
manually triggered Deploy Hook answers `{"job": {"state": "PENDING"}}` without
producing a build.

**Cause:** Vercel Hobby accounts may only run cron jobs **once per day**. A
schedule that fires more often is rejected at deploy time with:

```
Hobby accounts are limited to daily cron jobs. This cron expression
(0 */5 * * *) would run more than once per day. Upgrade to the Pro plan to
unlock all Cron Jobs features on Vercel.
```

A GitHub-triggered deployment that fails this validation is dropped **silently**,
which is why it reads as "the portal is still on the old code" rather than as
an error. Only `vercel --prod` from the CLI shows the message above.

**Fix:** every `schedule` in `vercel.json` must fire at most once a day. Run

```bash
python check_cron_schedules.py
```

before deploying. It parses every cron expression, rejects any that fires more
than once a day, and checks that each cron path still has a route in `routes/`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Production still serves the old build after a push | A cron expression fires more than once a day — Vercel drops the deployment silently | Run `python check_cron_schedules.py`, fix the schedule, redeploy. See "The cron schedule breaks the deployment" above |
| Production still serves the old build after a **successful** deploy | The deployment was a preview — a preview never changes the production alias | Run `vercel --prod`, or promote the deployment in the dashboard. Confirm with `curl -sI https://viama-three.vercel.app/cron/approved-survey-emails` — a `404` means the old build, `403` means the new one |
| `500` referencing `defect_report_email_sent_at` | The migration ran against the report copy, not production | `python check_database_schema.py`, then run the migration against production. See "The migration ran against the wrong database" above |
| `503 {"error":"upstream_unavailable"}` | `RV_DASHBOARD_TOKEN` missing, or the RoadVision VM is down | Check the env var is set **and** that you redeployed after adding it |
| `403 {"error":"forbidden"}` | `CRON_SECRET` mismatch | Value in Vercel must match what you send in `X-Cron-Secret` |
| Summary shows `considered: 0` | No surveys completed in the last 10 days | Widen with `?days=30` to confirm the query works |
| All rows `no_bucket_data` | Section numbering drift between the two systems | See "Known data issues" below |
| `500` referencing `video_count_matched` | Migration didn't run on this database | `python migrate_video_count_check.py` |
| `500` referencing survey date-approval or raw-email fields | Approval/email migration didn't run on this database | `python migrate_survey_date_approval_email.py` |
| `500` referencing `cancelled_survey_reports` | The weekly/cancellation report migration didn't run on this database | `python migrate_weekly_survey_reports.py` |
| Approved-survey email fails to send | Gmail OAuth identity is not `adordashcam@gmail.com` or a verified Send mail as alias | Reauthorize the Gmail token for that sender with `gmail.compose` scope, then redeploy |
| A defect-report XLSX email goes out when it should not | `DEFECT_REPORT_EMAIL` is set to `1` in Vercel | Set it back to `0` and redeploy. See "Defect-report mail is switched off" above |
| Cron never fires | Env vars added but not redeployed, or the cron was removed from `vercel.json` | Redeploy; check Settings → Cron Jobs → last run |
| Rescheduled survey never auto-cancelled | `RESCHEDULED_EXPIRY=0` is set, or it is already Sunday 23:30 IST and the sweep has not run | Check the tick's `rescheduled_expiry` result; `would_cancel: 0` with `candidates > 0` means the rows are still in date |

---

## Known data issues (not deployment blockers)

- **`section_no` is free text**, so joining to a bucket row is fuzzy. The matcher
  now tries the value as-is, zero-padded, and the single number embedded in it,
  so `'84(B)'` → `084` and `'133 re-survey'` → `133` match where they previously
  did not. Values naming *several* sections (`'1 & 2'`, `'7 & 33'`) are reported
  as `ambiguous_section` and declined rather than guessed — matching one of the
  two would produce a confidently wrong verdict. Cleaning `section_no` upstream
  is still the real fix.
- **Survey 354** (section 114, WC3) has `MCW.LHS` enlisted 38 / bucket 0 while
  `MCW.RHS` is 0 / 38 — a left/right inversion, worth a human look.
- **The token expires 2027-07-28.** Expiry returns `401`, not a warning. Put it in
  someone's calendar; a replacement has to come from the RoadVision side.
- The upstream API is **HTTP-only on a single VM with no failover** and does not
  survive a reboot without a manual restart. The sync serves a stale cache and
  fails closed, so a dead upstream never corrupts remarks — it just does nothing.

---

## Rollback

The feature is additive and safe to disable:

1. Remove the `crons` block from `vercel.json` and redeploy — the automation stops
   immediately.
2. The two DB columns and any written remarks can be left in place harmlessly.
3. To also clear generated remarks:

```sql
UPDATE surveys
SET roadvision_remark = NULL,
    video_count_matched = NULL,
    video_count_checked_at = NULL
WHERE video_count_checked_at IS NOT NULL;
```

> Note: hand-written remarks are protected — the sync skips any survey holding a
> remark it did not itself write (`skipped_manual_remark` in the summary).
