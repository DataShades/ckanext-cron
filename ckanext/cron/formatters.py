from __future__ import annotations

import ckan.plugins.toolkit as tk

import ckanext.tables.shared as t

from ckanext.cron.helpers import cron_explain_schedule

STATUS_STYLES = {
    "queued": "secondary",
    "running": "primary",
    "succeeded": "success",
    "failed": "danger",
}


def _badge(status: str) -> str:
    return tk.literal('<span class="badge bg-{}">{}</span>').format(STATUS_STYLES.get(status, "secondary"), status)


class ScheduleFormatter(t.formatters.BaseFormatter):
    """Show a cron schedule with a plain-words description under it."""

    def format(self, value: t.Value, options: t.Options) -> t.FormatterResult:
        if not value:
            return tk.literal('<span class="badge bg-warning text-dark">{}</span>').format(tk._("not scheduled"))

        explanation = cron_explain_schedule(value)

        if not explanation:
            return tk.literal("<code>{}</code>").format(value)

        # one wrapper element, because Tabulator lays out cell children in a row
        return tk.literal('<div><code>{}</code><div class="text-muted small text-wrap">{}</div></div>').format(
            value, explanation
        )


class CommandFormatter(t.formatters.BaseFormatter):
    """Show a job command the way it is typed in a shell."""

    def format(self, value: t.Value, options: t.Options) -> t.FormatterResult:
        if not value:
            return ""

        return tk.literal('<code class="text-wrap">ckan {}</code>').format(value)


class RunStatusFormatter(t.formatters.BaseFormatter):
    """Show a run status as a coloured badge."""

    def format(self, value: t.Value, options: t.Options) -> t.FormatterResult:
        return _badge(value) if value else ""


class LastRunFormatter(t.formatters.BaseFormatter):
    """Show the latest run of a job: status badge and when it was queued.

    Reads `last_status`, `last_created` and `trigger_error` from the row. A
    trigger error means pg_cron could not enqueue the run at all.
    """

    def format(self, value: t.Value, options: t.Options) -> t.FormatterResult:
        row = self.initial_row
        date_format = options.get("date_format", "%d/%m/%Y - %H:%M")

        if row.get("trigger_error"):
            return tk.literal(
                '<div><span class="badge bg-danger">{}</span><div class="small text-wrap">{}</div></div>'
            ).format(tk._("pg_cron failed"), row["trigger_error"])

        if not row.get("last_status"):
            return tk.literal('<span class="text-muted">{}</span>').format(tk._("Never"))

        when = tk.h.render_datetime(row["last_created"], date_format=date_format)

        return tk.literal('<div>{}<div class="text-muted small">{}</div></div>').format(_badge(row["last_status"]), when)


class DurationFormatter(t.formatters.BaseFormatter):
    """Show a duration given in seconds, such as 12 ms, 3.4 s or 2 min 5 s."""

    def format(self, value: t.Value, options: t.Options) -> t.FormatterResult:
        if value is None or value == "":
            return ""

        seconds = float(value)

        if seconds < 1:
            return tk._("{ms} ms").format(ms=round(seconds * 1000))

        if seconds < 60:
            return tk._("{s} s").format(s=round(seconds, 1))

        minutes, rest = divmod(round(seconds), 60)

        return tk._("{m} min {s} s").format(m=minutes, s=rest)
