"""
Telegram transport: sends a text message to one Telegram chat.

What it does
    `Notifier.send(text)` posts the text to the Telegram Bot API. Text
    longer than Telegram's limit is split at line breaks into several
    messages. Message wording is built elsewhere (core/alerts.py); this
    module only delivers it.

Channels
    Settings define two chats: `general` for day-to-day messages and `ops`
    for validation failures. Blocking validation failures go to `ops` only,
    so they are not buried among routine updates. `from_settings(conf,
    channel)` builds a Notifier for either one.

Failure handling
    Sending is best effort. `send` never raises: it returns False and
    stores the reason in `last_error`. A Telegram outage should not crash
    the validator and lose its report; the caller records that the alert
    failed, and can queue the message in core/outbox.py for a later retry.
"""

from __future__ import annotations

import httpx

# Telegram rejects messages over 4096 characters; stay a little under.
MAX_LEN = 4000


class Notifier:
    """Sends messages to one chat. `dry_run=True` records them in `sent`
    without calling Telegram, which tests and dry runs use."""

    def __init__(self, token, chat_id, timeout=15.0, dry_run=False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.http = httpx.Client(timeout=timeout)
        self.last_error = None
        self.sent = []
        # Telegram replies with the message it created (message_id, chat,
        # date). Keeping those receipts means delivery can be shown, with
        # a message id in a named chat, rather than only inferred from the
        # absence of an error.
        self.receipts = []

    def send(self, text, silent=False):
        """Send `text` (HTML parse mode). Returns True if Telegram accepted
        every chunk. Never raises; on failure `last_error` says why."""
        self.last_error = None
        chunks = self._split(text)
        if self.dry_run:
            self.sent.extend(chunks)
            return True
        if not self.token or not self.chat_id:
            self.last_error = "telegram token or chat id not configured"
            return False
        for chunk in chunks:
            try:
                resp = self.http.post(
                    "https://api.telegram.org/bot%s/sendMessage" % self.token,
                    json={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_notification": silent,
                    },
                )
                if resp.status_code >= 400:
                    self.last_error = "%s %s" % (resp.status_code, resp.text[:300])
                    return False
                self._record(resp)
            except Exception as exc:
                self.last_error = str(exc)
                return False
            self.sent.append(chunk)
        return True

    def _record(self, resp):
        """Store the message id and chat from Telegram's reply in `receipts`.

        A reply that cannot be parsed is skipped rather than raised: the
        message was accepted, and an unreadable receipt does not change
        that.
        """
        try:
            payload = resp.json().get("result") or {}
        except Exception:
            return
        chat = payload.get("chat") or {}
        self.receipts.append(
            {
                "message_id": payload.get("message_id"),
                "chat_id": chat.get("id"),
                "chat_title": chat.get("title") or chat.get("username") or "",
                "date": payload.get("date"),
                "text": payload.get("text", ""),
            }
        )

    @staticmethod
    def _split(text):
        """Split text into chunks of at most MAX_LEN characters, preferring
        to cut at a line break. Long validation reports are the messages
        that most need to arrive whole, so they are split, not truncated."""
        text = str(text)
        if len(text) <= MAX_LEN:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_LEN:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, MAX_LEN)
            if cut <= 0:
                cut = MAX_LEN
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        return chunks

    def close(self):
        self.http.close()


def from_settings(settings, channel="ops", dry_run=False):
    """Build a Notifier for the `general` or `ops` channel from settings."""
    conf = settings.telegram(channel)
    return Notifier(conf["token"], conf["chat_id"], conf["timeout"], dry_run=dry_run)
