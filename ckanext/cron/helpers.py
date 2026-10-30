from __future__ import annotations

import logging
import re

import ckan.plugins.toolkit as tk

from ckanext.cron import job_commands, pg_cron

log = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^(\d+)\s+seconds?$", re.IGNORECASE)


def cron_explain_schedule(schedule: str | None) -> str:
    """Describe a pg_cron schedule in words, or return "" if it can't be described."""
    if not schedule:
        return ""

    schedule = schedule.strip()
    macros = {
        "@reboot": tk._("When pg_cron starts"),
        "@hourly": tk._("Every hour"),
        "@daily": tk._("Every day at 00:00"),
        "@weekly": tk._("Every Sunday at 00:00"),
        "@monthly": tk._("On the first day of every month at 00:00"),
        "@yearly": tk._("Every year on 1 January at 00:00"),
        "@annually": tk._("Every year on 1 January at 00:00"),
    }

    if explanation := macros.get(schedule.lower()):
        return explanation

    if match := _INTERVAL_RE.match(schedule):
        return tk._("Every {seconds} seconds").format(seconds=match.group(1))

    parts = schedule.split()

    if len(parts) != 5:
        return ""

    # pg_cron writes "last day of the month" as $, cron-descriptor as L
    parts[2] = parts[2].replace("$", "L")

    try:
        from cron_descriptor import get_description
    except ImportError:
        return ""

    try:
        return get_description(" ".join(parts))
    except Exception:
        log.debug("Cannot describe cron schedule %r", schedule, exc_info=True)
        return ""


def cron_schedule_presets() -> list[dict[str, str]]:
    """Schedules suggested by the job form."""
    presets = [
        ("*/5 * * * *", tk._("Every 5 minutes")),
        ("*/30 * * * *", tk._("Every 30 minutes")),
        ("@hourly", tk._("Every hour")),
        ("0 3 * * *", tk._("Every day at 03:00")),
        ("0 3 * * 0", tk._("Every Sunday at 03:00")),
        ("0 3 1 * *", tk._("First day of every month at 03:00")),
        ("0 3 $ * *", tk._("Last day of every month at 03:00")),
        ("30 seconds", tk._("Every 30 seconds")),
    ]
    return [{"value": value, "text": text} for value, text in presets]


def cron_command_suggestions() -> list[str]:
    """Runnable `ckan` commands, for the command field of the job form."""
    try:
        return job_commands.leaf_commands()
    except Exception:
        log.exception("Cannot list ckan commands")
        return []


def cron_runs_url(job_id: str) -> str:
    """URL of the run history page, filtered down to one job."""
    from ckanext.cron.tables import CronRunTable

    name = CronRunTable.NAME

    return tk.url_for(
        "cron.runs",
        **{f"field-{name}": "job_id", f"operator-{name}": "=", f"value-{name}": job_id},
    )


def cron_grant_statement(username: str, functions: list[str] | None = None) -> str:
    return pg_cron.grant_statement(username, functions)
