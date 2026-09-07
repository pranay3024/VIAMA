"""
Daily dump client - COPY THIS FILE TO THE OTHER VM.

It is standalone: only the Python 3 standard library, no Flask, no SQLAlchemy,
nothing from this project.  Drop it anywhere on the receiving machine.

    python3 dump_client.py --url https://<portal> --token <API_TOKEN> --out /var/backups/viama

What it does
------------
1. Fetches ``/api/v1/dump/manifest`` and records expected row counts.
2. Streams ``/api/v1/dump?compress=gzip`` to a dated file, never holding the
   whole thing in memory.
3. Verifies the trailing ``_summary`` line against the manifest and fails loudly
   on a mismatch, so a truncated download cannot be mistaken for a good backup.
4. Writes ``latest.json`` with the ``next_since_map`` cursor, so ``--incremental``
   pulls only new rows on later runs.
5. Prunes dumps older than ``--keep-days``.

Exit codes: 0 success, 1 download/verify failure, 2 configuration error.
Non-zero means cron should alert you - a backup job that fails silently is worse
than no backup job.

Cron - every day at 12:00, as requested
---------------------------------------
    0 12 * * * /usr/bin/python3 /opt/viama/dump_client.py \\
        --url https://your-portal.vercel.app \\
        --token "$VIAMA_API_TOKEN" \\
        --out /var/backups/viama \\
        --keep-days 30 >> /var/log/viama-dump.log 2>&1

Put the token in a root-owned env file rather than the crontab itself - crontabs
are world-readable on many systems.
"""

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "viama-dump-client/1.0"
CHUNK = 1024 * 256


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def request_json(url, token, timeout=120):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url, token, destination, timeout=1800):
    """Stream to disk in chunks; returns (bytes_written, seconds)."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            # The body is already gzip from the server; don't let urllib
            # transparently re-negotiate encoding.
            "Accept-Encoding": "identity",
        },
    )
    started = time.time()
    written = 0
    temporary = destination + ".part"

    with urllib.request.urlopen(req, timeout=timeout) as response, open(
        temporary, "wb"
    ) as handle:
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)

    os.replace(temporary, destination)
    return written, time.time() - started


def read_summary(path):
    """
    Pull the trailing ``_summary`` line out of the dump.

    Its presence is the proof the stream completed - a truncated download simply
    will not have it.
    """
    opener = gzip.open if path.endswith(".gz") else open
    summary = None
    meta = None
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                if '"_summary"' in line:
                    summary = json.loads(line).get("_summary")
                elif '"_meta"' in line and meta is None:
                    meta = json.loads(line).get("_meta")
    except (OSError, ValueError) as exc:
        return None, None, f"unreadable dump: {exc}"
    return meta, summary, None


def verify(manifest, summary, incremental):
    """Compare what the server said it had against what actually arrived."""
    problems = []
    expected = {t["table"]: t.get("rows", 0) for t in manifest.get("tables", []) if "rows" in t}
    actual = {name: info["rows"] for name, info in (summary.get("tables") or {}).items()}

    for table, count in expected.items():
        if table not in actual:
            problems.append(f"{table}: missing from dump")
        elif not incremental and actual[table] != count:
            # On a full dump the counts must match. Rows written between the
            # manifest and the dump can make actual slightly larger - only a
            # shortfall is a real problem.
            if actual[table] < count:
                problems.append(
                    f"{table}: expected {count} rows, got {actual[table]}"
                )
    return problems


def prune(directory, keep_days):
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    for name in os.listdir(directory):
        if not name.startswith("viama_full_"):
            continue
        path = os.path.join(directory, name)
        try:
            modified = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            if modified < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True, help="portal base URL, no trailing slash")
    parser.add_argument("--token", default=os.getenv("VIAMA_API_TOKEN"), help="API bearer token")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--keep-days", type=int, default=30, help="0 disables pruning")
    parser.add_argument("--incremental", action="store_true",
                        help="pull only rows newer than the last run")
    parser.add_argument("--include-audit", action="store_true",
                        help="include login/access logs (contains IP addresses)")
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    if not args.token:
        log("ERROR: no token. Pass --token or set VIAMA_API_TOKEN.")
        return 2

    base = args.url.rstrip("/")
    os.makedirs(args.out, exist_ok=True)
    state_path = os.path.join(args.out, "latest.json")

    query = []
    if args.include_audit:
        query.append("include_audit=true")
    if not args.no_compress:
        query.append("compress=gzip")

    since_map = None
    if args.incremental and os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as handle:
                since_map = json.load(handle).get("next_since_map")
            if since_map:
                query.append("since_map=" + urllib.parse.quote(json.dumps(since_map)))
                log(f"incremental run, cursors: {since_map}")
        except (OSError, ValueError):
            log("WARNING: could not read latest.json; doing a full dump")

    # 1. manifest
    try:
        manifest_url = f"{base}/api/v1/dump/manifest"
        if args.include_audit:
            manifest_url += "?include_audit=true"
        manifest = request_json(manifest_url, args.token)["data"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        log(f"ERROR: manifest failed with HTTP {exc.code}: {body}")
        return 1
    except Exception as exc:
        log(f"ERROR: manifest failed: {exc}")
        return 1

    log(
        f"manifest: {len(manifest['tables'])} tables, "
        f"{manifest.get('total_rows', 0)} rows"
    )

    # 2. download
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = ".ndjson" if args.no_compress else ".ndjson.gz"
    destination = os.path.join(args.out, f"viama_full_{stamp}{suffix}")
    dump_url = f"{base}/api/v1/dump" + ("?" + "&".join(query) if query else "")

    try:
        size, seconds = download(dump_url, args.token, destination, args.timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        log(f"ERROR: dump failed with HTTP {exc.code}: {body}")
        return 1
    except Exception as exc:
        log(f"ERROR: dump failed: {exc}")
        return 1

    log(f"downloaded {size / 1024 / 1024:.2f} MB in {seconds:.1f}s -> {destination}")

    # 3. verify
    meta, summary, error = read_summary(destination)
    if error or not summary:
        log(f"ERROR: dump is incomplete or unreadable ({error or 'no _summary line'})")
        log("The file has been kept for inspection but must NOT be trusted as a backup.")
        return 1

    problems = verify(manifest, summary, args.incremental)
    if problems:
        log("ERROR: verification failed:")
        for problem in problems:
            log(f"  - {problem}")
        return 1

    log(
        f"verified: {summary['total_rows']} rows across "
        f"{len(summary['tables'])} tables in {summary.get('duration_seconds')}s"
    )

    # 4. record cursors for the next incremental run
    try:
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "last_dump": os.path.basename(destination),
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "total_rows": summary["total_rows"],
                    "bytes": size,
                    "next_since_map": summary.get("next_since_map", {}),
                    "meta": meta,
                },
                handle,
                indent=2,
            )
    except OSError as exc:
        log(f"WARNING: could not write {state_path}: {exc}")

    # 5. prune
    removed = prune(args.out, args.keep_days)
    if removed:
        log(f"pruned {removed} dump(s) older than {args.keep_days} days")

    log("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
