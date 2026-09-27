#!/usr/bin/env python3
"""Build a static, read-only copy of the dashboard for hosting.

What it does
    The dashboard normally reads three live services on this machine:
    PocketBase on port 8090, the project files server on port 8092, and
    Todoist. A hosted page can reach none of them, and the Todoist token
    must never leave this machine. So this script freezes the reads:

    1. Every PocketBase collection the dashboard uses (COLLECTIONS) is read
       in full.
    2. Every files server endpoint the dashboard calls is fetched and stored
       under the exact path and query string the page will ask for: the
       fixed ones (FIXED_ENDPOINTS), each project folder, each playbook spec
       and, unless --no-logs, the tail of each log file.
    3. The lot is written to <out>/snapshot.json, the page files
       (STATIC_FILES) are copied beside it, and a generated config.local.js
       switches on dashboard/snapshot_shim.js. The shim answers every read
       from snapshot.json and refuses every write, so the hosted copy has
       nothing behind it.
    4. A Cloudflare _headers file is written with the caching and security
       headers.

    Nothing here writes to PocketBase or Todoist.

Usage
    python3 -m tools.snapshot_build --out dist
    python3 -m tools.snapshot_build --pb http://127.0.0.1:8090 --files http://127.0.0.1:8092

bin/publish-dashboard runs this and then uploads dist/ to Cloudflare Pages.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "dashboard"

# Every collection the dashboard reads. A page that reads a collection
# missing from this list is blank on the hosted copy.
COLLECTIONS = [
    "clients",
    "contacts",
    "jobs",
    "quotes",
    "suppliers",
    "interactions",
    "knowledge_base",
    "project_financials",
    "project_financials_summary",
    "assignments",
    "equipment",
    "skills",
    "skill_refs",
    "skill_routes",
    "skill_invocations",
]

# Files server calls the dashboard makes that have no variable part.
FIXED_ENDPOINTS = [
    "/api/project-files",
    "/api/assignments",
    "/api/xero",
    "/api/logs",
    "/api/playbook/health",
    "/api/playbook/runs?limit=40",
    "/api/folder-contents?path=" + urllib.parse.quote("_howtotasks/playbooks", safe=""),
]

# The Logs page opens at 200 lines, so that is the tail worth capturing.
LOG_TAIL_LINES = 200

# The page and every local script it loads. index.html refuses to start
# when one of its engines is missing, so each local <script src> in
# dashboard/index.html must be listed here (tests/test_publish.py checks
# this). config.local.js is not copied: build() writes the hosted one.
STATIC_FILES = [
    "index.html",
    "quality_engine.js",
    "filter_engine.js",
    "streamer_engine.js",
    "snapshot_shim.js",
]


def log(msg):
    print("[snapshot] " + msg, flush=True)


def get_json(url, timeout=45):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def dump_collection(pb, name):
    """Page through a collection until every record is in hand."""
    out, page = [], 1
    while True:
        url = "%s/api/collections/%s/records?perPage=500&page=%d" % (pb, name, page)
        data = get_json(url)
        items = data.get("items") or []
        out.extend(items)
        if page >= (data.get("totalPages") or 1) or not items:
            return out
        page += 1


def capture(files_url, path_and_query, store, timeout=45):
    """Fetch one files server endpoint and store it under the exact key the
    dashboard will ask for. Returns True when the key is in the store.

    A failure is logged and skipped rather than raised, so one broken
    endpoint costs one panel of the hosted page, not the whole snapshot.
    """
    if path_and_query in store:
        return True
    try:
        store[path_and_query] = get_json(files_url + path_and_query, timeout=timeout)
        return True
    except Exception as exc:
        log("  skipped %s (%s)" % (path_and_query, exc))
        return False


def build(pb, files_url, out_dir, include_logs=True):
    started = dt.datetime.now(dt.timezone.utc)

    collections = {}
    for name in COLLECTIONS:
        try:
            rows = dump_collection(pb, name)
            collections[name] = rows
            log("  %-30s %5d records" % (name, len(rows)))
        except Exception as exc:
            collections[name] = []
            log("  %-30s FAILED (%s)" % (name, exc))

    endpoints = {}
    for ep in FIXED_ENDPOINTS:
        capture(files_url, ep, endpoints)

    # Every project file's own folder, so the Files browser and the
    # financials document scanner both have something to show.
    project_files = endpoints.get("/api/project-files") or []
    folders = set()
    for entry in project_files:
        path = (entry or {}).get("path") or ""
        if "/" in path:
            folders.add(path.rsplit("/", 1)[0])
    for folder in sorted(folders):
        capture(files_url,
                "/api/folder-contents?path=" + urllib.parse.quote(folder, safe=""),
                endpoints, timeout=20)
    log("  captured %d project folders" % len(folders))

    # The playbook spec files, opened by clicking a step on the Playbooks page.
    pb_folder = endpoints.get(
        "/api/folder-contents?path=" + urllib.parse.quote("_howtotasks/playbooks", safe=""))
    specs = 0
    for entry in (pb_folder or {}).get("entries", []) if isinstance(pb_folder, dict) else []:
        name = (entry or {}).get("name") or ""
        if name.endswith(".spec.json"):
            rel = "_howtotasks/playbooks/" + name
            if capture(files_url,
                       "/api/file?path=" + urllib.parse.quote(rel, safe=""),
                       endpoints, timeout=20):
                specs += 1
    log("  captured %d playbook specs" % specs)

    if include_logs:
        tails = 0
        for entry in endpoints.get("/api/logs") or []:
            name = (entry or {}).get("file")
            if not name:
                continue
            q = "/api/logs/tail?name=%s&lines=%d" % (
                urllib.parse.quote(name, safe=""), LOG_TAIL_LINES)
            if capture(files_url, q, endpoints, timeout=20):
                tails += 1
        log("  captured %d log tails" % tails)

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "generated_at": started.isoformat().replace("+00:00", "Z"),
        "source": "splatt-ops snapshot_build",
        "collections": collections,
        "endpoints": endpoints,
    }
    snap_path = out / "snapshot.json"
    snap_path.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")

    for name in STATIC_FILES:
        src = DASHBOARD / name
        if not src.exists():
            log("  MISSING %s, the hosted page will not work without it" % name)
            continue
        shutil.copy2(src, out / name)

    # The hosted config. No Todoist token, and the two service addresses use
    # the reserved .invalid domain, so any request the shim does not
    # intercept fails at once instead of trying the viewer's own localhost.
    (out / "config.local.js").write_text(
        "// Generated by tools/snapshot_build.py. Do not edit by hand.\n"
        "// This is the hosted, read only config. It carries no Todoist token\n"
        "// and the two addresses below are sentinels the shim matches on,\n"
        "// never anything it actually calls.\n"
        "window.SPLATT_CONFIG = {\n"
        '  SNAPSHOT_URL: "snapshot.json",\n'
        '  PB_URL: "https://snapshot.invalid/pb",\n'
        '  FILES_URL: "https://snapshot.invalid/files",\n'
        '  TODOIST_TOKEN: "",\n'
        "};\n",
        encoding="utf-8")

    # Cloudflare Pages reads _headers. snapshot.json is never cached, so a
    # new publish is seen at once, and the page may not be framed.
    #
    # Referrer-Policy is strict-origin-when-cross-origin rather than
    # no-referrer because OpenStreetMap's tile servers answer requests with
    # no Referer with an "Access blocked" tile. This policy sends OSM the
    # site's origin only, never the path or query.
    (out / "_headers").write_text(
        "/*\n"
        "  X-Frame-Options: DENY\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "  X-Robots-Tag: noindex, nofollow\n"
        "/snapshot.json\n"
        "  Cache-Control: no-store\n",
        encoding="utf-8")

    size = snap_path.stat().st_size
    log("wrote %s (%.1f MB)" % (snap_path, size / 1024.0 / 1024.0))
    log("bundle ready at %s" % out)
    return snapshot


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pb", default=os.environ.get("PB_URL", "http://localhost:8090"))
    ap.add_argument("--files", default=os.environ.get("FILES_URL", "http://localhost:8092"))
    ap.add_argument("--out", default=str(REPO / "dist"))
    ap.add_argument("--no-logs", action="store_true",
                    help="leave the daemon log tails out of the snapshot")
    args = ap.parse_args()

    pb = args.pb.rstrip("/")
    files_url = args.files.rstrip("/")
    log("PocketBase %s" % pb)
    log("files server %s" % files_url)

    try:
        get_json(pb + "/api/health", timeout=10)
    except Exception as exc:
        log("PocketBase is not answering (%s). Start the stack first." % exc)
        return 2

    build(pb, files_url, args.out, include_logs=not args.no_logs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
