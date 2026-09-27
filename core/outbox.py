"""
Telegram outbox: a folder of queued messages that the daemon delivers later.

What it does
    A message that could not be sent is written as one small JSON file in
    `state/telegram-outbox/`. On every pass, the daemon loop calls
    `deliver()`, which tries each file and deletes it only after Telegram
    accepts it. The result is at-least-once delivery: a message may, in a
    rare case, arrive twice, but it is not silently lost.

Why it exists
    Direct sends fail for two ordinary reasons:
      1. some environments cannot reach api.telegram.org at all (for
         example an agent session behind an egress proxy that blocks it),
         and
      2. sends from the host itself fail now and then with a connection
         reset or TLS handshake timeout.
    In both cases the sender can still write a file into the repo's
    `state/` folder, and the daemon loop, which runs continuously on a
    machine that can reach Telegram, picks it up within one pass.

Entry format (one JSON object per file)

    {
      "queued_at":  ISO timestamp,
      "channel":    "general" | "ops",
      "type":       message type label, used in log lines only,
      "text":       the full, already formatted message,
      "parse_mode": "Markdown" | "HTML" | "",
      "silent":     bool,
      "attempts":   int,
      "last_error": str,
      "source":     who queued it
    }

    `skills/splatt-ss-agent/scripts/send_telegram.py` writes the same
    format without importing this module; tests/test_outbox.py checks
    that the two stay compatible.

Failure handling
    Like core/notify.py, delivery is best effort and never raises, so a
    Telegram outage cannot stop the sync loop. A file that keeps failing
    stays in the folder with its attempt count raised, and the number
    still queued is reported in the loop heartbeat.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

DIRNAME = "telegram-outbox"

# After this many failed attempts a file is still kept (a notification is
# never dropped), but its log line is raised from warning to error so a
# permanently stuck entry is noticed.
LOUD_AFTER = 10


def outbox_dir(repo_root) -> Path:
    return Path(repo_root) / "state" / DIRNAME


def enqueue(repo_root, text, channel="general", msg_type="info",
            parse_mode="", silent=False, source="") -> Path:
    """Queue one message and return the path of the file written.

    The file is written to a temporary name and then renamed, so
    `deliver()` never reads a half-written entry. Odd input values are
    coerced to strings rather than rejected (an unknown channel becomes
    "general"), because a message that is awkward to queue should still be
    queued. An OSError is allowed to propagate: if the disk cannot be
    written, the caller needs to know the message went nowhere.
    """
    folder = outbox_dir(repo_root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = folder / ("%s-%s.json" % (stamp, uuid.uuid4().hex[:8]))
    entry = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel if channel in ("general", "ops") else "general",
        "type": str(msg_type),
        "text": str(text),
        "parse_mode": str(parse_mode or ""),
        "silent": bool(silent),
        "attempts": 0,
        "last_error": "",
        "source": str(source),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _send_via_api(token, chat_id, entry, timeout):
    """Send one entry through the Telegram Bot API. Returns (ok, error)."""
    payload = {
        "chat_id": chat_id,
        "text": entry["text"][:4000],
        "disable_notification": bool(entry.get("silent")),
    }
    if entry.get("parse_mode"):
        payload["parse_mode"] = entry["parse_mode"]
    try:
        resp = httpx.post(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    if resp.status_code >= 400:
        body = resp.text[:300]
        # A 400 with a parse_mode set is usually malformed Markdown/HTML.
        # That will fail on every retry and block the queue, so try once
        # more as plain text.
        if resp.status_code == 400 and entry.get("parse_mode"):
            entry = dict(entry, parse_mode="")
            return _send_via_api(token, chat_id, entry, timeout)
        return False, "%s %s" % (resp.status_code, body)
    return True, ""


def deliver(conf, log, send_fn=None):
    """Try to send every queued file, oldest first. Called by the daemon loop.

    For each file:
      1. read it (an unreadable file is logged, counted and left alone),
      2. look up the token and chat for its channel from settings,
      3. send it (through `send_fn` if given, which tests use),
      4. delete it on success, or rewrite it with `attempts` raised and
         `last_error` set on failure.

    Returns {"delivered": n, "failed": n, "queued": remaining}. Never raises.
    """
    folder = outbox_dir(conf.repo_root)
    summary = {"delivered": 0, "failed": 0, "queued": 0}
    if not folder.exists():
        return summary

    # Credentials are looked up once per channel per pass.
    creds = {}

    def credentials(channel):
        if channel not in creds:
            try:
                c = conf.telegram(channel)
                creds[channel] = (c["token"], c["chat_id"], c["timeout"])
            except Exception as exc:
                creds[channel] = None
                log.error("outbox: no %s channel credentials, %s", channel, exc)
        return creds[channel]

    for path in sorted(folder.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("outbox: unreadable entry %s, %s", path.name, exc)
            summary["failed"] += 1
            summary["queued"] += 1
            continue

        channel = entry.get("channel", "general")
        got = credentials(channel)
        if not got:
            summary["failed"] += 1
            summary["queued"] += 1
            continue
        token, chat_id, timeout = got

        if send_fn is not None:
            ok, error = send_fn(token, chat_id, entry, timeout)
        else:
            ok, error = _send_via_api(token, chat_id, entry, timeout)

        if ok:
            try:
                path.unlink()
            except OSError:
                pass
            summary["delivered"] += 1
            log.info("outbox: delivered %s (%s → %s channel, queued %s)",
                     path.name, entry.get("type", "?"), channel,
                     entry.get("queued_at", "?"))
            continue

        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_error"] = error
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
        summary["failed"] += 1
        summary["queued"] += 1
        if entry["attempts"] >= LOUD_AFTER:
            log.error("outbox: %s still undelivered after %d attempts, %s",
                      path.name, entry["attempts"], error)
        else:
            log.warning("outbox: %s not delivered (attempt %d), %s",
                        path.name, entry["attempts"], error)

    return summary
