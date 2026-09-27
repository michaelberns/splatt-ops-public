#!/usr/bin/env python3
"""Record every dashboard publish as one row in the publish_log collection.

What it does
    Each run of bin/publish-dashboard gets a row saying when it started and
    finished, who triggered it, whether it worked, how fresh the published
    data was, how big the snapshot was, where it was deployed, and the tail
    of the output. The dashboard's Publish page reads that collection, so
    the history (failed scheduled runs included) is on screen.

Usage (bin/publish-dashboard makes these calls)
    id=$(python3 -m tools.publish_log start --trigger dashboard)
    python3 -m tools.publish_log finish --id "$id" --status success --duration 42
    python3 -m tools.publish_log stats --snapshot dist/snapshot.json

    `start` prints the new row id, or nothing if PocketBase could not be
    reached. `finish` with an empty id does nothing. `stats` prints the
    snapshot's timestamp, record count, collection count and size as JSON.

Design
    Recording must never break a publish. Every PocketBase error is caught,
    printed to stderr as a warning, and the command still exits 0: a
    publish that was not recorded is a small problem, a publish that did
    not happen because the recording failed is a big one.

    Writes use the superuser login from the environment or .env
    (PB_ADMIN_EMAIL, PB_ADMIN_PASSWORD). The dashboard can read
    publish_log without signing in, but only a superuser can write to it,
    so nothing else can add a publish that never ran.

    The PocketBase address comes from PB_URL (default
    http://localhost:8090). SPLATT_ENV_FILE points at a different .env.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import socket
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
COLLECTION = "publish_log"


def warn(msg):
    print("[publish-log] " + msg, file=sys.stderr, flush=True)


def env(name, default=""):
    """A setting from the environment, else from .env, else `default`.

    bin/publish-dashboard sources .env before calling this, so the
    environment is normally enough. Reading the file as well lets the
    module run by hand from a plain shell.
    """
    if os.environ.get(name):
        return os.environ[name]
    path = pathlib.Path(os.environ.get("SPLATT_ENV_FILE") or (REPO / ".env"))
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return default


def pb_url():
    return env("PB_URL", "http://localhost:8090").rstrip("/")


def request(method, path, body=None, token=None, timeout=20):
    req = urllib.request.Request(
        pb_url() + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def auth():
    """A superuser token, or None. Never raises."""
    email = env("PB_ADMIN_EMAIL")
    password = env("PB_ADMIN_PASSWORD")
    if not email or not password:
        warn("PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD are not set, so this publish "
             "will not be recorded. The publish itself is unaffected.")
        return None
    try:
        resp = request("POST", "/api/collections/_superusers/auth-with-password",
                       {"identity": email, "password": password})
        return resp.get("token")
    except Exception as exc:
        warn("could not sign in to PocketBase (%s). Not recording this run." % exc)
        return None


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000Z")


def cmd_start(args):
    """Print the new record id on stdout, or nothing at all.

    An empty id is a valid answer and the caller must cope with it, because
    PocketBase being down is not a reason to refuse to publish. finish
    simply does nothing when it is handed one.
    """
    token = auth()
    if not token:
        return 0
    record = {
        "started": now(),
        "status": "running",
        "trigger": args.trigger,
        "host": socket.gethostname(),
    }
    try:
        created = request("POST", "/api/collections/%s/records" % COLLECTION,
                          record, token=token)
        print(created.get("id", ""))
    except Exception as exc:
        warn("could not open the log row (%s)." % exc)
    return 0


def tail(path, lines=60, cap=8000):
    if not path:
        return ""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    out = "\n".join(text.splitlines()[-lines:])
    return out[-cap:]


def cmd_finish(args):
    if not args.id:
        return 0
    token = auth()
    if not token:
        return 0
    patch = {
        "finished": now(),
        "status": args.status,
        "output": tail(args.output_file),
    }
    for key, value in (
        ("snapshot_at", args.snapshot_at),
        ("deploy_url", args.deploy_url),
        ("error", args.error),
    ):
        if value:
            patch[key] = value
    for key, value in (
        ("records", args.records),
        ("collections", args.collections),
        ("size_kb", args.size_kb),
        ("duration_s", args.duration),
    ):
        if value is not None:
            patch[key] = value
    try:
        request("PATCH", "/api/collections/%s/records/%s" % (COLLECTION, args.id),
                patch, token=token)
    except Exception as exc:
        warn("could not close the log row (%s)." % exc)
    return 0


def cmd_stats(args):
    """Read dist/snapshot.json and print the numbers the finish call wants.

    Kept here rather than inlined in the shell script so there is one place
    that knows the shape of a snapshot file.
    """
    path = pathlib.Path(args.snapshot)
    try:
        raw = path.read_text(encoding="utf-8")
        snap = json.loads(raw)
    except Exception as exc:
        warn("could not read %s (%s)" % (path, exc))
        return 1
    collections = snap.get("collections") or {}
    records = sum(len(v or []) for v in collections.values())
    print(json.dumps({
        "generated_at": snap.get("generated_at", ""),
        "records": records,
        "collections": len(collections),
        "size_kb": int(path.stat().st_size / 1024),
    }))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="open a running row, print its id")
    s.add_argument("--trigger", default="terminal",
                   choices=["dashboard", "cron", "terminal", "ops"])
    s.set_defaults(func=cmd_start)

    f = sub.add_parser("finish", help="close the row opened by start")
    f.add_argument("--id", default="")
    f.add_argument("--status", required=True, choices=["success", "failed"])
    f.add_argument("--snapshot-at", dest="snapshot_at", default="")
    f.add_argument("--deploy-url", dest="deploy_url", default="")
    f.add_argument("--error", default="")
    f.add_argument("--records", type=int, default=None)
    f.add_argument("--collections", type=int, default=None)
    f.add_argument("--size-kb", dest="size_kb", type=int, default=None)
    f.add_argument("--duration", type=int, default=None)
    f.add_argument("--output-file", dest="output_file", default="")
    f.set_defaults(func=cmd_finish)

    st = sub.add_parser("stats", help="print snapshot numbers as JSON")
    st.add_argument("--snapshot", default=str(REPO / "dist" / "snapshot.json"))
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
