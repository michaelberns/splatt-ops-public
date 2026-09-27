"""
Tests for the Telegram outbox (core/outbox.py).

Behaviour pinned down:
  - a queued message stays queued until Telegram accepts it,
  - delivery deletes a file only after a successful send,
  - a failed send leaves the file in place with `attempts` raised and
    `last_error` set, and the next pass retries it,
  - an unreadable file does not stop the rest of the queue,
  - the channel in the entry decides which credentials are used,
  - the fallback writer in send_telegram.py produces entries that
    `deliver()` can read.

Sending is replaced by `send_fn`, so no test touches the network.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import outbox


log = logging.getLogger("test_outbox")


class FakeConf:
    def __init__(self, root):
        self.repo_root = Path(root)

    def telegram(self, channel):
        return {"token": "tok-%s" % channel,
                "chat_id": "chat-%s" % channel,
                "timeout": 5.0}


def test_enqueue_writes_a_readable_entry(tmp_path):
    path = outbox.enqueue(tmp_path, "hello", channel="ops",
                          msg_type="alert", source="test")
    assert path.exists()
    entry = json.loads(path.read_text())
    assert entry["text"] == "hello"
    assert entry["channel"] == "ops"
    assert entry["attempts"] == 0


def test_enqueue_unknown_channel_falls_back_to_general(tmp_path):
    path = outbox.enqueue(tmp_path, "x", channel="nonsense")
    assert json.loads(path.read_text())["channel"] == "general"


def test_deliver_deletes_only_on_success(tmp_path):
    conf = FakeConf(tmp_path)
    ok_path = outbox.enqueue(tmp_path, "will send", channel="general")
    bad_path = outbox.enqueue(tmp_path, "will fail", channel="ops")

    def send_fn(token, chat_id, entry, timeout):
        if entry["text"] == "will send":
            assert token == "tok-general"
            return True, ""
        assert token == "tok-ops"
        return False, "boom"

    summary = outbox.deliver(conf, log, send_fn=send_fn)
    assert summary["delivered"] == 1
    assert summary["queued"] == 1
    assert not ok_path.exists()
    assert bad_path.exists()
    assert json.loads(bad_path.read_text())["attempts"] == 1
    assert json.loads(bad_path.read_text())["last_error"] == "boom"


def test_failed_entry_is_retried_next_pass(tmp_path):
    conf = FakeConf(tmp_path)
    path = outbox.enqueue(tmp_path, "flaky", channel="general")
    calls = []

    def fail_then_pass(token, chat_id, entry, timeout):
        calls.append(1)
        return (len(calls) > 1), "reset by peer"

    outbox.deliver(conf, log, send_fn=fail_then_pass)
    assert path.exists()
    outbox.deliver(conf, log, send_fn=fail_then_pass)
    assert not path.exists()


def test_unreadable_file_does_not_stop_the_queue(tmp_path):
    conf = FakeConf(tmp_path)
    folder = outbox.outbox_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "00-garbage.json").write_text("{not json")
    good = outbox.enqueue(tmp_path, "fine", channel="general")

    summary = outbox.deliver(conf, log,
                             send_fn=lambda *a: (True, ""))
    assert summary["delivered"] == 1
    assert not good.exists()
    assert (folder / "00-garbage.json").exists()


def test_empty_or_missing_outbox_is_a_noop(tmp_path):
    conf = FakeConf(tmp_path)
    summary = outbox.deliver(conf, log,
                             send_fn=lambda *a: (True, ""))
    assert summary == {"delivered": 0, "failed": 0, "queued": 0}


def test_send_telegram_fallback_entry_is_deliverable(tmp_path, monkeypatch):
    """send_telegram.py writes outbox entries without importing core (it
    also runs outside the repo), so its format is checked against the
    reader here. A mismatch would leave queued messages undeliverable."""
    script_dir = (Path(__file__).resolve().parents[1]
                  / "skills" / "splatt-ss-agent" / "scripts")
    sys.path.insert(0, str(script_dir))
    try:
        import send_telegram
    finally:
        sys.path.remove(str(script_dir))

    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=x\n")
    monkeypatch.setattr(send_telegram, "_find_env_file", lambda: env_file)

    written = send_telegram._queue_fallback("📋 formatted", "ops_summary",
                                            "ops", silent=False)
    assert written is not None

    conf = FakeConf(tmp_path)
    seen = []

    def send_fn(token, chat_id, entry, timeout):
        seen.append(entry)
        return True, ""

    summary = outbox.deliver(conf, log, send_fn=send_fn)
    assert summary["delivered"] == 1
    assert seen[0]["channel"] == "ops"
    assert seen[0]["text"] == "📋 formatted"
