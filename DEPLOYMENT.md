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

`.env.example` lists every variable the app reads, including the optional ones.

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
vercel --prod
```

or push to the branch connected to the Vercel project.

---

## 3. Confirm the cron registered

**Vercel dashboard → your project → Settings → Cron Jobs.**

You should see:

| Path | Schedule |
|---|---|
| `/cron/video-count-sync` | `0 2 * * *` |

That is **02:00 UTC = 07:30 IST**, daily.

> **Hobby plan allows exactly one cron invocation per day.** The schedule above is
> already within that limit. If you upgrade to Pro and want the shortfalls to
> self-heal faster as uploads land, change the `schedule` in `vercel.json` to
> e.g. `0 */6 * * *` (every 6 hours) and redeploy.

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 {"error":"upstream_unavailable"}` | `RV_DASHBOARD_TOKEN` missing, or the RoadVision VM is down | Check the env var is set **and** that you redeployed after adding it |
| `403 {"error":"forbidden"}` | `CRON_SECRET` mismatch | Value in Vercel must match what you send in `X-Cron-Secret` |
| Summary shows `considered: 0` | No surveys completed in the last 10 days | Widen with `?days=30` to confirm the query works |
| All rows `no_bucket_data` | Section numbering drift between the two systems | See "Known data issues" below |
| `500` referencing `video_count_matched` | Migration didn't run on this database | `python migrate_video_count_check.py` |
| Cron never fires | Env vars added but not redeployed, or Hobby daily cap already used | Redeploy; check Settings → Cron Jobs → last run |

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
