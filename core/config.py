"""
Configuration: one settings file and one secrets file for the whole system.

What it does
    Every port, URL, path, Todoist id and interval is read from
    `config/settings.yaml`. Every secret (PocketBase admin login, Todoist
    token, Telegram bot tokens) is read from environment variables, which
    can be supplied by a single `.env` file at the repo root. The YAML only
    names the variable to read (for example `admin_email_env:
    PB_ADMIN_EMAIL`), so the settings file holds no secrets and is safe to
    commit.

How it is used
    1. Code calls `settings()` to get the process-wide `Settings` object.
       The YAML is parsed once, and the `.env` is loaded into `os.environ`
       without overwriting variables that are already set.
    2. Callers ask for a value by dotted key (`get("todoist.project_id")`)
       or through a service helper such as `pocketbase()` or
       `telegram("ops")`, which returns the keyword arguments for that
       client.
    3. A missing setting or secret raises `ConfigError` naming the exact
       key and file, so the problem is reported where it is instead of
       surfacing later as an unrelated failure.

Why a single loader
    Nothing else in the codebase hardcodes a port, path or id. Moving an
    installation means editing one YAML file and one `.env`, and
    `missing_secrets()` lets the validator's `doctor` command list every
    unset secret before a run starts.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
ENV_PATH = REPO_ROOT / ".env"


# Sentinel for "no default given", so that None can still be a real default.
_MISSING = object()


class ConfigError(Exception):
    """A setting or secret is missing or malformed. The message names it."""


def load_env(path=ENV_PATH):
    """Load KEY=value lines from the .env file into os.environ.

    Blank lines and `#` comments are skipped and surrounding quotes are
    stripped. A variable that is already set in the real environment is
    never overwritten, so a value exported by a service manager or CI
    takes priority over the file. Returns the parsed pairs, or an empty
    dict when the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


class Settings:
    """Access to settings.yaml, with secrets read from the environment.

    Every accessor raises `ConfigError` naming the missing key rather than
    returning None. A None handed onwards tends to fail much later, in
    code that has nothing to do with configuration, which hides the real
    cause.
    """

    def __init__(self, settings_path=SETTINGS_PATH, env_path=ENV_PATH):
        self.repo_root = REPO_ROOT
        self.settings_path = Path(settings_path)
        if not self.settings_path.exists():
            raise ConfigError("settings file not found at %s" % self.settings_path)
        self.data = yaml.safe_load(self.settings_path.read_text()) or {}
        load_env(env_path)

    # generic lookups
    def get(self, dotted, default=_MISSING):
        """Fetch a nested value by dotted key.

        >>> settings().get("pocketbase.timeout_seconds")   # 15
        >>> settings().get("no.such.key", None)            # None

        Raises `ConfigError` when the key is absent and no default is given.
        """
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise ConfigError("missing setting '%s' in %s" % (dotted, self.settings_path))
                return default
            node = node[part]
        return node

    def secret(self, env_var, required=True):
        """Read a secret from the environment variable named `env_var`.

        When `required` is true and the variable is unset or empty, raises
        `ConfigError` naming the variable and the .env file to add it to.
        """
        value = os.environ.get(env_var, "")
        if not value and required:
            raise ConfigError(
                "missing secret '%s'. Add it to %s" % (env_var, ENV_PATH)
            )
        return value

    def path(self, dotted):
        """Resolve a path setting. `~` is expanded, and a relative path is
        joined onto `root()`."""
        raw = str(self.get(dotted))
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        return self.root() / candidate

    def root(self):
        """The base folder for relative path settings.

        This is `paths.root`, unless `paths.root_env_override` names an
        environment variable (for example SPLATT_ROOT) and that variable is
        set, in which case its value is used.
        """
        override_var = self.get("paths.root_env_override", "")
        if override_var:
            override = os.environ.get(override_var, "")
            if override:
                return Path(override).expanduser()
        return Path(str(self.get("paths.root"))).expanduser()

    def _url(self, section):
        """The base URL for a service section, without a trailing slash.

        If the section names a `url_env_override` variable and that
        variable is set, its value wins over `url`. The same settings file
        then works on the host that runs the services (localhost) and on
        another machine that has to reach them by network address.
        """
        override_var = self.get("%s.url_env_override" % section, "")
        if override_var:
            override = os.environ.get(override_var, "")
            if override:
                return override.rstrip("/")
        return str(self.get("%s.url" % section)).rstrip("/")

    # service helpers: each returns the keyword arguments for that client
    def pocketbase(self):
        return {
            "base_url": self._url("pocketbase"),
            "admin_email": self.secret(self.get("pocketbase.admin_email_env")),
            "admin_password": self.secret(self.get("pocketbase.admin_password_env")),
            "timeout": float(self.get("pocketbase.timeout_seconds", 15)),
            "enforce_schema": bool(self.get("pocketbase.enforce_schema", True)),
        }

    def playbook(self):
        return {
            "base_url": self._url("playbook"),
            "timeout": float(self.get("playbook.timeout_seconds", 15)),
        }

    def todoist(self):
        return {
            "token": self.secret(self.get("todoist.token_env")),
            "project_id": self.get("todoist.project_id"),
            "sections": self.get("todoist.sections"),
            "timeout": float(self.get("todoist.timeout_seconds", 15)),
        }

    def telegram(self, channel="ops"):
        return {
            "token": self.secret(self.get("telegram.%s.token_env" % channel)),
            "chat_id": self.secret(self.get("telegram.%s.chat_id_env" % channel)),
            "timeout": float(self.get("telegram.timeout_seconds", 15)),
        }

    # diagnostics
    def missing_secrets(self):
        """Names of every secret the config refers to that is not set.

        Used by `python -m validator doctor`, so a misconfiguration is
        reported before a run starts rather than part way through one.
        """
        wanted = [
            self.get("pocketbase.admin_email_env"),
            self.get("pocketbase.admin_password_env"),
            self.get("todoist.token_env"),
            self.get("telegram.general.token_env"),
            self.get("telegram.general.chat_id_env"),
            self.get("telegram.ops.token_env"),
            self.get("telegram.ops.chat_id_env"),
        ]
        return [name for name in wanted if not os.environ.get(name)]


_settings = None


def settings():
    """The process-wide `Settings`, created on first use so the YAML is
    parsed and the .env loaded only once."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
