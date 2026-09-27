"""
Notifications: sends the `actions` messages from the rules file to the ops Telegram channel.

What it does
    Every validation run announces itself and reports its outcome, pass or
    fail. A clean run sends a message too, so a quiet channel means the
    validator stopped running, never that it had nothing to say.

    on_start          before the first rule is evaluated
    on_block          once per blocking failure, throttled
    on_warn           once per warning, throttled
    on_block_summary  the closing message of a blocked run, never throttled
    on_pass           the closing message of a run with nothing blocking
    on_complete       a run that ended without a verdict (crash, no database)

    Each hook's `telegram` entries have a `template` whose {placeholders}
    are filled from the fields cli.py passes in. A placeholder with no
    value is rendered as "{name: not recorded}" instead of raising.

Throttling
    A `throttle` block on an entry caps how many messages it sends per run:
    `max_per_run` per group (`group_by: rule_id` makes that one per rule)
    and `max_total_per_run` across the whole run. Messages held back are
    counted, and the counts are reported: in on_block_summary for a blocked
    run, or in one "held back" message before on_pass for a clean run.

The heartbeat and silence_is_a_fault
    Each send that Telegram accepts rewrites state/validation-heartbeat.json.
    `silence_check` compares its age with `notifications.silence_is_a_fault.
    max_quiet_hours`. `python -m validator heartbeat` and the daemon loop use
    it to raise an alert when the channel has been quiet too long. A failed
    send does not refresh the heartbeat, so broken Telegram credentials show
    up as silence rather than as health.

What is and is not wired
    Executed by this module: the `telegram` entries under the six hooks
    above, their `template` and their `throttle` (group_by, max_per_run,
    max_total_per_run). cli.py reads `notifications.channel`.

    Not executed by any code, kept in the YAML as reserved or descriptive
    entries: `todoist_task`, `log_interaction`, `write_to_validation_report`,
    the whole `on_bypass` hook, `silence_is_a_fault.on_trip` (only printed
    by the heartbeat command), `when`, `summarise_remainder` (the remainder
    is always summarised), `always_send`, `msg_type` and
    `message_must_answer`. `agent_may_report_success` and
    `agent_must_surface_in_summary` describe behaviour that comes from the
    exit code and the JSON report instead.

Sending is best effort, like core/notify.py: a Telegram failure is logged
and never changes the report or the exit code.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEARTBEAT_NAME = "validation-heartbeat.json"

# What a template placeholder shows when the run has no value for it.
# Showing the gap is better than inventing a plausible number.
UNSOURCED = "not recorded"


def heartbeat_path(conf):
    return Path(conf.repo_root) / "state" / HEARTBEAT_NAME


def write_heartbeat(conf, run_id, hook):
    """Record that Telegram accepted a validation message.

    Called on accepted sends only. If a failed send refreshed the
    heartbeat, expired Telegram credentials would look like a healthy
    validator.
    """
    path = heartbeat_path(conf)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_message_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "hook": hook,
    }
    path.write_text(json.dumps(payload, indent=2))
    return payload


def quiet_for_hours(conf, now=None):
    """Hours since the last accepted ops message, or None if none was ever sent.

    None is different from zero: a missing or unreadable heartbeat means
    no message has ever been confirmed, which callers treat as at least as
    serious as a stale one.
    """
    path = heartbeat_path(conf)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        last = datetime.fromisoformat(data["last_message_at"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() / 3600.0


def silence_check(conf, actions_config, now=None):
    """Evaluate silence_is_a_fault. Returns (tripped, message).

    `actions_config` is the `notifications` block of the rules file. This
    only reports; the caller decides what to do about a trip. It is a
    plain function so the daemon can call it without building a Dispatcher.
    """
    cfg = (actions_config or {}).get("silence_is_a_fault") or {}
    if not cfg.get("enabled"):
        return False, "silence_is_a_fault is not enabled"
    limit = float(cfg.get("max_quiet_hours", 24))
    hours = quiet_for_hours(conf, now=now)
    if hours is None:
        return True, (
            "No validation message has ever reached the ops channel. "
            "There is no heartbeat at %s." % heartbeat_path(conf)
        )
    if hours > limit:
        return True, (
            "The ops channel has seen no validation message for %.1f hours, "
            "and the limit is %.0f." % (hours, limit)
        )
    return False, "Last ops message was %.1f hours ago, within the %.0f hour limit." % (
        hours,
        limit,
    )


class _SafeFields(dict):
    """Renders an unknown {placeholder} as "{name: not recorded}" instead of
    raising KeyError, so a template naming a field nobody supplies still
    produces a message."""

    def __missing__(self, key):
        return "{%s: %s}" % (key, UNSOURCED)


def render(template, fields):
    """Fill a template's {placeholders} from fields.

    >>> render("Run: {run_id}, took {duration_seconds}s", {"run_id": "abc"})
    'Run: abc, took {duration_seconds: not recorded}s'
    """
    if not template:
        return ""
    try:
        return str(template).format_map(_SafeFields(fields)).strip()
    except (ValueError, IndexError):
        # A stray brace in the template. Send the raw text, which still
        # carries the verdict, rather than nothing.
        return str(template).strip()


class Dispatcher:
    """Sends the `telegram` entries of the actions block for each hook.

    Entries of any other type are ignored (see "What is and is not wired"
    in the module docstring). With `enabled=False` the dispatcher records
    what it would have sent in `self.sent` without contacting Telegram.
    """

    def __init__(self, notifier, actions, conf, run_id, log=None, enabled=True):
        self.notifier = notifier
        self.actions = actions or {}
        self.notifications = (self.actions or {}).get("_notifications") or {}
        self.conf = conf
        self.run_id = run_id
        self.log = log
        self.enabled = enabled
        self.sent = []
        self.failures = []
        # Keyed by (hook, group): how many messages were requested and how
        # many were offered to Telegram. The difference is what the
        # throttle held back. (hook, None) holds the per-hook totals.
        self._asked = {}
        self._offered = {}

    # helpers
    def _entries(self, hook):
        """The telegram entries under a hook, in file order."""
        out = []
        for entry in self.actions.get(hook) or []:
            if isinstance(entry, dict) and "telegram" in entry:
                out.append(entry)
        return out

    def _deliver(self, hook, text):
        """Send one message. Returns True if accepted; never raises."""
        if not text:
            return False
        if not self.enabled:
            self.sent.append((hook, text))
            return True
        ok = False
        try:
            ok = self.notifier.send(text)
        except Exception as exc:  # a failed send must never end the run
            self.failures.append("%s: %s" % (hook, exc))
            if self.log:
                self.log.error("telegram %s raised %s", hook, exc)
            return False
        if ok:
            self.sent.append((hook, text))
            write_heartbeat(self.conf, self.run_id, hook)
        else:
            err = getattr(self.notifier, "last_error", "unknown error")
            self.failures.append("%s: %s" % (hook, err))
            if self.log:
                self.log.error("telegram %s failed: %s", hook, err)
        return ok

    def fire(self, hook, fields):
        """Render and send every telegram entry under a hook, unthrottled.

        There is deliberately no "only if something changed" condition:
        the clean-run message is the one that proves the validator ran.
        """
        results = []
        for entry in self._entries(hook):
            text = render(entry.get("template"), fields)
            results.append(self._deliver(hook, text))
        return any(results) if results else False

    # hooks
    def on_start(self, fields):
        return self.fire("on_start", fields)

    def on_pass(self, fields):
        return self.fire("on_pass", fields)

    def on_complete(self, fields):
        return self.fire("on_complete", fields)

    def on_block_summary(self, fields):
        """The closing message of a blocked run. Sent once and never
        throttled, and it carries the counts of every blocking failure,
        including those the throttle held back."""
        return self.fire("on_block_summary", fields)

    def on_block(self, fields):
        """One message per blocking failure, subject to the YAML throttle
        (by default one per rule and three per run). on_block_summary
        carries the full count."""
        return self._fire_throttled("on_block", fields)

    def on_warn(self, fields):
        """One message per warning, subject to the YAML throttle. A warn
        rule can fail on hundreds of records, so this is normally capped
        at one message per rule."""
        return self._fire_throttled("on_warn", fields)

    def _fire_throttled(self, hook, fields):
        """Send unless this run has reached the entry's throttle limits.

        `max_per_run` limits one group (normally one rule, via
        `group_by: rule_id`), so twenty failures of one rule read as one
        problem. `max_total_per_run` limits the hook across the whole run.
        Both are optional; an entry with no throttle block sends every
        time. Every message not sent is counted in `_asked` so it can be
        reported by held_back().
        """
        sent_any = False
        for entry in self._entries(hook):
            throttle = entry.get("throttle") or {}
            group = fields.get(throttle.get("group_by") or "", "_all") or "_all"
            key = (hook, group)
            total_key = (hook, None)
            self._asked[key] = self._asked.get(key, 0) + 1
            self._asked[total_key] = self._asked.get(total_key, 0) + 1

            per_group = int(throttle.get("max_per_run", 0) or 0)
            per_run = int(throttle.get("max_total_per_run", 0) or 0)
            if per_group and self._offered.get(key, 0) >= per_group:
                continue
            if per_run and self._offered.get(total_key, 0) >= per_run:
                continue

            self._offered[key] = self._offered.get(key, 0) + 1
            self._offered[total_key] = self._offered.get(total_key, 0) + 1
            if self._deliver(hook, render(entry.get("template"), fields)):
                sent_any = True
        return sent_any

    def held_back(self, hook=None):
        """Messages the throttle held back, per group, e.g. {"gate_won": 19}.

        The per-hook totals under the None group are skipped so nothing is
        counted twice.
        """
        out = {}
        for (this_hook, group), asked in self._asked.items():
            if group is None or (hook and this_hook != hook):
                continue
            gap = asked - self._offered.get((this_hook, group), 0)
            if gap > 0:
                out[group] = out.get(group, 0) + gap
        return out

    def held_back_line(self, hook=None):
        """held_back() as one phrase for a template field. Never empty."""
        remainder = self.held_back(hook)
        if not remainder:
            return "nothing, every message was sent"
        return ", ".join("%s +%d more" % (k, v) for k, v in sorted(remainder.items()))

    def summarise_suppressed(self):
        """On a clean run, send one message listing what the throttle held back.

        Blocked runs do not call this, because on_block_summary already
        carries the same counts.
        """
        remainder = self.held_back()
        if not remainder:
            return None
        parts = ", ".join("%s +%d more" % (k, v) for k, v in sorted(remainder.items()))
        text = "🔕 Further validation messages held back this run\nRun: %s\n%s" % (
            self.run_id,
            parts,
        )
        return text if self._deliver("summary", text) else None
