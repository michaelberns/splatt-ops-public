#!/usr/bin/env python3
"""
Telegram notifier for the SS agent.

What it does
    Sends one formatted message to one of two Telegram channels, and if the
    direct send fails, queues the message in the repo's outbox so the
    daemon loop delivers it later. This script is the only way the agent
    sends Telegram messages.

Channels
    general (logged as "regular")  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    ops                             TELEGRAM_BOT_TOKEN_OPS / TELEGRAM_CHAT_ID_OPS

    The message type decides the channel: "ops_summary" and "validation"
    go to ops, everything else goes to general.

Where the credentials come from, highest priority first
    1. The process environment, if a variable is already exported. This is
       how the daemon hands credentials to a child process without writing
       them anywhere.
    2. The file named by SPLATT_ENV_FILE, if that variable is set.
    3. The repo's own .env, found from this file's location
       (skills/splatt-ss-agent/scripts/ is three folders below the repo root).

    The .env file is only ever read, never written.

The outbox fallback
    If Telegram cannot be reached or rejects the message, the message is
    written as a JSON file to state/telegram-outbox/ beside the .env that
    was used (the repo root in the normal setup). The daemon loop drains
    that folder on every pass and retries until Telegram accepts, so
    delivery is at least once. A queued message counts as sent.

Usage
    python send_telegram.py "Your message here"
    python send_telegram.py --type ops_summary "Summary text"     # ops channel
    python send_telegram.py --type validation "Validation passed" # ops channel
    python send_telegram.py --type project_update "Updated ..."   # general channel
    python send_telegram.py --type alert "INV-0001 overdue"       # general channel

Message types and the title line each one gets
    ops_summary    -> 📋 SS Ops Summary   (ops channel)
    validation     -> 🔎 Validation       (ops channel)
    project_update -> 📝 Project Update
    invoice        -> 💰 Invoice
    alert          -> 🚨 Alert
    task           -> ✅ Task Update
    info           -> ℹ️ Info
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# skills/splatt-ss-agent/scripts/send_telegram.py -> the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

ENV_CANDIDATES: list[Path] = []
if os.environ.get("SPLATT_ENV_FILE"):
    ENV_CANDIDATES.append(Path(os.environ["SPLATT_ENV_FILE"]))
ENV_CANDIDATES.append(REPO_ROOT / ".env")


def _find_env_file() -> Path | None:
    """Return the first .env candidate that exists, or None.

    With SPLATT_ENV_FILE unset this is simply <repo>/.env when it exists.
    """
    for p in ENV_CANDIDATES:
        if p.exists():
            return p
    return None


TYPE_PREFIXES = {
    "ops_summary":    "📋 *SS Ops Summary*",
    "validation":     "🔎 *Validation*",
    "project_update": "📝 *Project Update*",
    "invoice":        "💰 *Invoice*",
    "alert":          "🚨 *Alert*",
    "task":           "✅ *Task Update*",
    "info":           "ℹ️ *Info*",
}

# Message types routed to the ops channel. Validation results and the end
# of run summary live there, so the general channel stays for client and
# job traffic and the ops channel shows whether the system is still running.
OPS_CHANNEL_TYPES = {"ops_summary", "validation"}


def _parse_env_file(path: Path) -> dict:
    """Minimal KEY=value reader, so the script needs no dotenv dependency."""
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve(env_vars: dict, key: str) -> str | None:
    """Prefer the process environment, fall back to the .env file."""
    return os.environ.get(key) or env_vars.get(key)


def load_credentials(use_ops_channel: bool) -> tuple[str, str, str]:
    """Return (bot_token, chat_id, channel_label) for the requested channel.

    channel_label is "regular" or "ops" and is only used in log lines.
    Exits with a message naming every place it looked if a value is missing.
    """
    env_vars: dict = {}
    env_path = _find_env_file()
    if env_path is not None:
        env_vars = _parse_env_file(env_path)

    checked_paths = [str(p) for p in ENV_CANDIDATES]

    if use_ops_channel:
        token = _resolve(env_vars, "TELEGRAM_BOT_TOKEN_OPS")
        chat_id = _resolve(env_vars, "TELEGRAM_CHAT_ID_OPS")
        label = "ops"
        if not token or not chat_id:
            sys.exit(
                "❌ TELEGRAM_BOT_TOKEN_OPS or TELEGRAM_CHAT_ID_OPS not set.\n"
                f"   Checked these paths and the process environment:\n"
                f"     {checked_paths}\n"
                "   Add them to .env to enable the ops channel."
            )
    else:
        token = _resolve(env_vars, "TELEGRAM_BOT_TOKEN")
        chat_id = _resolve(env_vars, "TELEGRAM_CHAT_ID")
        label = "regular"
        if not token or not chat_id:
            sys.exit(
                "❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.\n"
                f"   Checked these paths and the process environment:\n"
                f"     {checked_paths}"
            )

    return token, chat_id, label


def _queue_fallback(full_message: str, msg_type: str, label: str,
                    silent: bool) -> Path | None:
    """Write the message into the Telegram outbox for the daemon loop.

    The entry format is the one core/outbox.py reads. This function is
    deliberately free of repo imports, because the script can also run
    from an installed copy of the skill where the repo's core package is
    not importable.

    Returns the path written, or None if no .env (and so no repo root)
    could be found.
    """
    env_path = _find_env_file()
    if env_path is None:
        return None
    outbox = env_path.parent / "state" / "telegram-outbox"
    try:
        outbox.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        suffix = os.urandom(4).hex()
        path = outbox / f"{stamp}-{suffix}.json"
        entry = {
            # Same string as the naive UTC isoformat plus "+00:00".
            "queued_at": now.isoformat(),
            # settings.yaml calls the day to day channel "general"; this
            # script logs it as "regular". The outbox uses the settings name.
            "channel": "ops" if label == "ops" else "general",
            "type": msg_type,
            "text": full_message,
            "parse_mode": "Markdown",
            "silent": bool(silent),
            "attempts": 0,
            "last_error": "",
            "source": "send_telegram.py",
        }
        # Write to a temporary name and rename, so the deliverer never
        # reads a half written file.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def send_message(text: str, msg_type: str = "info", silent: bool = False) -> dict:
    """Post one message to Telegram, on the channel chosen by msg_type."""
    use_ops = msg_type in OPS_CHANNEL_TYPES
    token, chat_id, label = load_credentials(use_ops_channel=use_ops)

    prefix = TYPE_PREFIXES.get(msg_type, "ℹ️ *Info*")
    timestamp = datetime.now().strftime("%H:%M · %d %b %Y")
    full_message = f"{prefix}\n_{timestamp}_\n\n{text}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": full_message,
        "parse_mode": "Markdown",
        "disable_notification": silent,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"✅ Telegram message sent ({msg_type} → {label} channel)")
                return result
            print(f"❌ Telegram API error ({label} channel): {result}")
            error = str(result)
    except Exception as exc:
        error = str(exc)
        print(f"❌ Direct send failed ({label} channel): {exc}")

    # The direct send failed, so hand the message to the daemon loop.
    queued_path = _queue_fallback(full_message, msg_type, label, silent)
    if queued_path is not None:
        print(f"📬 Queued for delivery by the daemon loop: {queued_path.name}")
        print("   The loop drains the outbox every pass (about 60s). "
              "Delivery is at least once.")
        return {"ok": True, "queued": True, "path": str(queued_path),
                "direct_error": error}
    print("❌ Could not queue either, no .env and so no repo root found. "
          "Message NOT delivered.")
    return {"ok": False, "error": error}


def main() -> None:
    parser = argparse.ArgumentParser(description="SS agent Telegram notifier")
    parser.add_argument("message", help="The message body to send")
    parser.add_argument(
        "--type", "-t",
        choices=list(TYPE_PREFIXES.keys()),
        default="info",
        help="Message type (sets the channel and the title line)",
    )
    parser.add_argument(
        "--silent", "-s",
        action="store_true",
        help="Send without a notification sound",
    )
    args = parser.parse_args()

    result = send_message(args.message, msg_type=args.type, silent=args.silent)
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
