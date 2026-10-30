from __future__ import annotations

import re

from sqlalchemy.engine import make_url

import ckan.plugins.toolkit as tk

CONF_DATABASE_URL = "ckanext.cron.database_url"
CONF_POLL_INTERVAL = "ckanext.cron.worker.poll_interval"
CONF_TIMEOUT = "ckanext.cron.worker.timeout"
CONF_EXECUTABLE = "ckanext.cron.worker.executable"
CONF_HISTORY_RETENTION_DAYS = "ckanext.cron.history_retention_days"
CONF_ALLOWED_COMMANDS = "ckanext.cron.allowed_commands"
CONF_FORBIDDEN_COMMANDS = "ckanext.cron.forbidden_commands"


def database_url() -> str:
    """URL of the database that holds the pg_cron extension."""
    return tk.config.get(CONF_DATABASE_URL) or tk.config["sqlalchemy.url"]


def ckan_database() -> str:
    """Name of the CKAN database, where pg_cron jobs enqueue runs."""
    return make_url(tk.config["sqlalchemy.url"]).database or ""


def poll_interval() -> int:
    return tk.config[CONF_POLL_INTERVAL]


def timeout() -> int:
    return tk.config[CONF_TIMEOUT]


def executable() -> str | None:
    return tk.config.get(CONF_EXECUTABLE) or None


def history_retention_days() -> int:
    return tk.config[CONF_HISTORY_RETENTION_DAYS]


def allowed_commands() -> list[tuple[str, ...]]:
    """Command prefixes that jobs are limited to. Empty means any command."""
    return _command_list(CONF_ALLOWED_COMMANDS)


def forbidden_commands() -> list[tuple[str, ...]]:
    """Command prefixes that jobs may not run."""
    return _command_list(CONF_FORBIDDEN_COMMANDS)


def _command_list(key: str) -> list[tuple[str, ...]]:
    """Parse a list of commands separated by commas or newlines, such as `db, user token`."""
    value = tk.config.get(key) or ""
    return [tuple(item.split()) for item in re.split(r"[,\n]", value) if item.strip()]
