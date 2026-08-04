# RoadVision video-counts API — upstream contract

The authoritative per-bucket video counts that `utils/video_count_sync.py`
compares against. Referenced from `.env` and from the module docstring.

This documents the upstream as VIAMA consumes it. It is **not** a VIAMA endpoint
and nothing in this repository serves it — it runs on a separate RoadVision VM.

---

## 1. Endpoint

| | |
|---|---|
| **URL** | `http://34.180.38.204:7001/api/video-counts` |
| **Method** | `GET` |
| **Auth** | `Authorization: Bearer <RV_DASHBOARD_TOKEN>` |
| **Override** | `RV_VIDEO_COUNTS_URL` env var |
| **Typical latency** | ~45 ms |
| **Client timeout** | 15 s (`FETCH_TIMEOUT_SECONDS`) |

> ⚠️ **This is plain HTTP, not HTTPS.** The bearer token crosses the network in
> cleartext. The host is a single unmanaged VM with no failover that does not
> restart its service automatically after a reboot. Treat availability as
> best-effort — the sync is built to tolerate it being down (§6).

---

## 2. Authentication

```bash
curl -s http://34.180.38.204:7001/api/video-counts \
  -H "Authorization: Bearer $RV_DASHBOARD_TOKEN"
```

The token is a JWT issued by the RoadVision side:

| Claim | Value |
|---|---|
| `sub` | `external-site` |
| `scope` | `video-counts` |
| `iat` | 1785228994 — 2026-07-26 |
| `exp` | 1816764994 — **2027-07-28 08:56:34 UTC** |

**Expiry is a hard cliff.** The upstream answers an expired token with a plain
`401` and no advance warning. VIAMA now reads the `exp` claim locally and logs a
warning from 30 days out (`utils.video_count_sync.check_token_expiry`); the
countdown also appears in the `/cron/video-count-sync` summary as
`token_days_remaining`. A replacement must be issued by the RoadVision side —
VIAMA cannot mint one.

---

## 3. Response shape

```json
{
  "row_count": 204,
  "last_scan_completed": "2026-07-29T01:31:07Z",
  "rows": [
    {
      "section": "114",
      "cycle": "WC3",
      "counts": {
        "MCW": { "LHS": 38, "RHS": 0  },
        "SR":  { "LHS": 4,  "RHS": 4  },
        "SL":  { "LHS": 0,  "RHS": 0  },
        "IR":  { "LHS": 2,  "RHS": 2  }
      }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `row_count` | Number of entries in `rows`. |
| `last_scan_completed` | When the upstream last scanned the GCS bucket. It rescans every ~30 s. |
| `rows[].section` | Section identifier. Usually a zero-padded 3-digit number (`"084"`); occasionally a full UPC code (`"N/09064/02001/JH"`). |
| `rows[].cycle` | Work cycle, formatted `WC<n>` — `WC3` for `cycle_no = 3`. |
| `rows[].counts` | Videos actually present in the bucket, by road type and side. |

`(section, cycle)` is unique upstream — verified against all 204 live rows.

### Road-type keys

| Key | Road type | VIAMA columns |
|---|---|---|
| `MCW` | Main Carriageway | `mcw_lhs_count`, `mcw_rhs_count` |
| `SR` | Service Road | `service_lhs_count`, `service_rhs_count` |
| `SL` | Slip Road | `slip_lhs_count`, `slip_rhs_count` |
| `IR` | Intersection Road | `ir_lhs_count`, `ir_rhs_count` |

Each has `LHS` and `RHS` sub-keys. All eight cells are always present.

---

## 4. The matching rule

A survey **passes** when, for all eight cells:

```
enlisted_on_portal  <=  present_in_bucket
```

Surplus is fine — the bucket may legitimately hold duplicate or trailing clips.
A shortfall (`portal > bucket`) means videos are genuinely missing from GCS and
is the only failure this checks for.

---

## 5. Joining a survey to a row

`surveys.section_no` is **free text** on the VIAMA schema, so the join is more
involved than it looks. `utils.video_count_sync.section_candidates` tries, in
order:

1. The value as-is — `"114"`.
2. Zero-padded to 3 — `"9"` → `"009"`.
3. The single number embedded in it — `"84(B)"` → `"84"` → `"084"`,
   `"133 re-survey"` → `"133"`.
4. Failing all of those, a separator-stripped compare of `surveys.upc_code`
   against `rows[].section`, so `N_09064_02001_JH` matches `N/09064/02001/JH`.

**Values naming more than one section are summed, not guessed.** `"1 & 2"` and
`"7 & 33"` cover two stretches with two separate bucket rows. The survey enlists
its videos across both, so when *every* named section has a bucket row the check
compares the enlisted count against their **total** — which is exactly the rule
this module already applies to a single section (the bucket may hold more, never
fewer). `combined_from` on the synthetic row records which sections were added,
and the remark says so.

**A partial set is never summed.** If `"1 & 2"` resolves section 1 but not
section 2, adding what we have would understate the bucket and manufacture a
shortfall that is not real. Those stay declined as `ambiguous_section`, but the
remark now names the section that has no bucket row — actionable, unlike the old
"please split this survey" wording.

A value with several numbers but no conjunction — a UPC code — is *not* ambiguous
and takes the UPC path.

Measured against live data (243 completed surveys, 2026-08-03): 233 get a
definitive verdict (95.9%), up from 229 (94.2%) before summing. The 10 that do
not are 7 multi-section surveys with a genuinely missing bucket row and 3 rows
for `N/02021/01015/UP`, which the upstream does not carry under any spelling.

Cleaning `section_no` upstream remains the real fix.

---

## 6. Failure behaviour

The sync is built to fail closed. A dead upstream never corrupts remarks.

| Condition | Behaviour |
|---|---|
| `RV_DASHBOARD_TOKEN` unset | Logs an error, returns `None`, sync writes nothing. |
| Upstream `401` (token expired) | Logged with the response body; stale cache served if available, else nothing written. |
| Upstream down / timeout | Last good response served from cache; if the cache is empty, the sync reports `error: upstream_unavailable` and the endpoint answers `503`. |
| Response missing `rows` | Treated as a fetch failure. |

Responses are cached for 30 s (`CACHE_TTL_SECONDS`), matching the upstream's own
scan interval — polling faster returns identical data.

---

## 7. Consuming it from VIAMA

The comparison runs from **two** places, and both are needed.

### 7.1 On completion — immediate

`utils/video_count_hook.py` listens on the SQLAlchemy session and fires the
moment a survey's status becomes `completed`, whichever path did it: the captain
flow, the JSON API, or a shell. The remark is on the survey before the captain's
redirect lands.

It runs inside that request, so it is deliberately impatient — a 4 s upstream
timeout (`INLINE_FETCH_TIMEOUT_SECONDS`) rather than the usual 15. If the
upstream is slow or down it gives up silently and leaves the survey to the cron.
It writes on its own session, so a failure here can never roll back the
completion that triggered it.

Set `VIDEO_COUNT_ON_COMPLETE=0` to disable it without a deploy.

| Precondition | Behaviour |
|---|---|
| status → `completed` **and** `video_uploaded` | Checked, remark written. |
| status → anything else | Ignored. |
| `completed` without uploaded video | Skipped — nothing to compare. |
| Hand-written remark already present | Left alone. |
| Upstream unreachable | Completion succeeds, no remark, cron picks it up. |

### 7.2 Nightly cron — the thing that makes it correct

The immediate check is about latency, not truth. At the instant counts are
submitted the GCS upload is often still in flight, so the first verdict is
frequently a shortfall that resolves itself minutes later. The cron's rolling
window re-checks until it passes, which is what actually converges.

```
GET /cron/video-count-sync            # runs the comparison, writes remarks
GET /cron/video-count-sync?dry_run=1  # computes everything, commits nothing
GET /cron/video-count-sync?days=30    # widen the rolling window (default 10)
```

Auth: `X-Cron-Secret: <CRON_SECRET>`, `Authorization: Bearer <CRON_SECRET>`,
`?secret=`, or an active admin session. See [DEPLOYMENT.md](DEPLOYMENT.md).

Summary keys:

| Key | Meaning |
|---|---|
| `considered` | Surveys in the window. |
| `skipped_already_matched` | Already passing; not re-checked. |
| `skipped_manual_remark` | Hand-written remark, left alone. |
| `checked` | Actually compared this run. |
| `matched` | Passed. |
| `shortfall` | Bucket holds fewer videos than enlisted. |
| `no_bucket_data` | No matching row upstream. |
| `ambiguous_section` | `section_no` names several sections and at least one has no bucket row; declined. Sections that all resolve are summed and counted under `matched` / `shortfall`. |
| `token_days_remaining` | Days until `RV_DASHBOARD_TOKEN` expires. |
| `error` | `upstream_unavailable` when the fetch failed. |

---

## 8. Known upstream data issues

- **Section-number drift.** Some VIAMA `section_no` values have no upstream
  counterpart under any candidate form. These get the "no video data found"
  remark rather than a false mismatch.
- **Survey 354** (section 114, cycle WC3) reports `MCW.LHS` enlisted 38 / bucket
  0 while `MCW.RHS` is 0 / 38 — a left/right inversion on one side or the other.
  Worth a human look; the sync reports it as a shortfall, which is correct given
  the data it is handed.
