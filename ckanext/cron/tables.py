from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from typing_extensions import Self

import ckan.plugins.toolkit as tk
from ckan import model
from ckan.types import Context

import ckanext.tables.shared as t

from ckanext.cron import config, jobs, pg_cron
from ckanext.cron import formatters as cf
from ckanext.cron.model import CronJob, CronRun

DATETIME_FORMAT = "%d %b %Y %H:%M:%S"


def _call_action(name: str, data_dict: dict[str, Any]) -> tuple[Any, str | None]:
    """Call an action as the current user. Return its result, or the error message if it fails."""
    try:
        return tk.get_action(name)({"user": tk.current_user.name}, data_dict), None
    except tk.ValidationError as e:
        summary = e.error_summary or {}
        return None, "; ".join(str(v) for v in summary.values()) or str(e)
    except (tk.ObjectNotFound, tk.NotAuthorized) as e:
        return None, str(e) or type(e).__name__


def _result(error: str | None, message: str) -> t.ActionHandlerResult:
    if error:
        return t.ActionHandlerResult(success=False, error=error)

    return t.ActionHandlerResult(success=True, message=message)


class CronTableMixin:
    @classmethod
    def check_access(cls, context: Context) -> None:
        tk.check_access("cron_manage", {"user": tk.current_user.name})


class CronJobDataSource(t.ListDataSource):
    """All jobs, with their pg_cron schedule and latest run flattened into each row.

    Rows are loaded on first use, so that building the table never touches pg_cron.
    """

    def __init__(self):
        super().__init__([])
        self._loaded = False

    def filter(self, filters: list[t.FilterItem]) -> Self:
        if not self._loaded:
            self.data = self._load()
            self._loaded = True

        return super().filter(filters)

    def _load(self) -> list[dict[str, Any]]:
        try:
            job_list = jobs.dictize_jobs(list(model.Session.scalars(sa.select(CronJob).order_by(CronJob.name))))
        except pg_cron.PgCronError as e:
            raise t.DataSourceError(str(e)) from e

        return [
            {
                "id": job["id"],
                "name": job["name"],
                "command": job["command"],
                "schedule": job["schedule"],
                "active": job["active"],
                "trigger_error": job["trigger_error"],
                "last_status": job["last_run"]["status"] if job["last_run"] else None,
                "last_created": job["last_run"]["created"] if job["last_run"] else None,
            }
            for job in job_list
        ]


class CronJobTable(CronTableMixin, t.TableDefinition):
    NAME = "cron_jobs"

    def __init__(self):
        super().__init__(
            name=self.NAME,
            table_template="cron/jobs.html",
            data_source=CronJobDataSource(),
            placeholder=tk._("No cron jobs yet"),
            page_size=25,
            columns=[
                t.ColumnDefinition(field="id", visible=False, filterable=False),
                t.ColumnDefinition(field="name", min_width=160),
                t.ColumnDefinition(
                    field="command",
                    min_width=220,
                    formatters=[(cf.CommandFormatter, {})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(
                    field="schedule",
                    min_width=170,
                    formatters=[(cf.ScheduleFormatter, {})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(
                    field="active",
                    width=90,
                    resizable=False,
                    filterable=False,
                    tabulator_formatter="tickCross",
                    horizontal_align="center",
                ),
                t.ColumnDefinition(
                    field="last_status",
                    title=tk._("Last run"),
                    width=180,
                    formatters=[(cf.LastRunFormatter, {"date_format": DATETIME_FORMAT})],
                    tabulator_formatter="html",
                ),
            ],
            row_actions=[
                t.RowActionDefinition(
                    action="run_now",
                    label=tk._("Run now"),
                    icon="fa fa-play",
                    callback=self.row_action_run_now,
                ),
                t.RowActionDefinition(
                    action="edit",
                    label=tk._("Edit"),
                    icon="fa fa-pencil",
                    callback=self.row_action_edit,
                ),
                t.RowActionDefinition(
                    action="runs",
                    label=tk._("Run history"),
                    icon="fa fa-clock-rotate-left",
                    callback=self.row_action_runs,
                ),
                t.RowActionDefinition(
                    action="toggle_active",
                    label=tk._("Enable / disable"),
                    icon="fa fa-power-off",
                    callback=self.row_action_toggle_active,
                ),
                t.RowActionDefinition(
                    action="delete",
                    label=tk._("Delete"),
                    icon="fa fa-trash",
                    attrs={"class": "text-danger"},
                    callback=self.row_action_delete,
                    with_confirmation=True,
                ),
            ],
            bulk_actions=[
                t.BulkActionDefinition(
                    action="enable",
                    label=tk._("Enable selected"),
                    icon="fa fa-play",
                    callback=lambda rows: self._bulk_update(rows, active=True),
                    with_confirmation=False,
                ),
                t.BulkActionDefinition(
                    action="disable",
                    label=tk._("Disable selected"),
                    icon="fa fa-pause",
                    callback=lambda rows: self._bulk_update(rows, active=False),
                ),
                t.BulkActionDefinition(
                    action="delete",
                    label=tk._("Delete selected"),
                    icon="fa fa-trash",
                    attrs={"class": "text-danger"},
                    callback=self.bulk_action_delete,
                ),
            ],
        )

    def row_action_run_now(self, row: t.Row) -> t.ActionHandlerResult:
        run, error = _call_action("cron_job_trigger", {"id": row["id"]})
        return _result(error, tk._("Run {id} has been queued.").format(id=run["id"] if run else ""))

    def row_action_edit(self, row: t.Row) -> t.ActionHandlerResult:
        return t.ActionHandlerResult(success=True, redirect=tk.url_for("cron.jobs", edit=row["id"]))

    def row_action_runs(self, row: t.Row) -> t.ActionHandlerResult:
        return t.ActionHandlerResult(success=True, redirect=tk.h.cron_runs_url(row["id"]))

    def row_action_toggle_active(self, row: t.Row) -> t.ActionHandlerResult:
        job, error = _call_action("cron_job_show", {"id": row["id"]})

        if error:
            return _result(error, "")

        _, error = _call_action("cron_job_update", {"id": job["id"], "active": not job["active"]})
        message = tk._("The job has been disabled.") if job["active"] else tk._("The job has been enabled.")

        return _result(error, message)

    def row_action_delete(self, row: t.Row) -> t.ActionHandlerResult:
        _, error = _call_action("cron_job_delete", {"id": row["id"]})
        return _result(error, tk._("The job has been deleted."))

    def _bulk_update(self, rows: list[t.Row], **fields: Any) -> t.ActionHandlerResult:
        errors = [
            f"{row['name']}: {error}"
            for row in rows
            if (error := _call_action("cron_job_update", {"id": row["id"], **fields})[1])
        ]
        return _result("\n".join(errors), tk._("{n} jobs have been updated.").format(n=len(rows)))

    def bulk_action_delete(self, rows: list[t.Row]) -> t.ActionHandlerResult:
        errors = [
            f"{row['name']}: {error}" for row in rows if (error := _call_action("cron_job_delete", {"id": row["id"]})[1])
        ]
        return _result("\n".join(errors), tk._("{n} jobs have been deleted.").format(n=len(rows)))


class CronRunTable(CronTableMixin, t.TableDefinition):
    NAME = "cron_runs"

    def __init__(self):
        stmt = (
            sa.select(
                CronRun.id,
                CronRun.job_id,
                CronJob.name.label("job_name"),
                CronJob.command,
                CronRun.trigger,
                model.User.name.label("triggered_by"),
                CronRun.status,
                CronRun.created,
                CronRun.started,
                CronRun.finished,
                sa.extract("epoch", CronRun.finished - CronRun.started).label("duration"),
                CronRun.exit_code,
                CronRun.stdout,
                CronRun.stderr,
                CronRun.worker,
            )
            .join(CronJob, CronJob.id == CronRun.job_id)
            .outerjoin(model.User, model.User.id == CronRun.triggered_by)
            .order_by(CronRun.id.desc())
        )

        super().__init__(
            name=self.NAME,
            table_template="cron/runs.html",
            data_source=t.DatabaseDataSource(stmt),
            placeholder=tk._("No runs yet"),
            page_size=25,
            columns=[
                t.ColumnDefinition(field="id", title=tk._("Run"), width=90, resizable=False),
                t.ColumnDefinition(field="job_id", visible=False),
                t.ColumnDefinition(field="job_name", title=tk._("Job"), min_width=150),
                t.ColumnDefinition(
                    field="command",
                    min_width=180,
                    visible=False,
                    formatters=[(cf.CommandFormatter, {})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(field="trigger", width=110),
                t.ColumnDefinition(field="triggered_by", title=tk._("By"), width=130),
                t.ColumnDefinition(
                    field="status",
                    width=120,
                    formatters=[(cf.RunStatusFormatter, {})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(
                    field="created",
                    title=tk._("Queued"),
                    width=180,
                    formatters=[(t.formatters.DateFormatter, {"date_format": DATETIME_FORMAT})],
                ),
                t.ColumnDefinition(
                    field="started",
                    width=180,
                    visible=False,
                    formatters=[(t.formatters.DateFormatter, {"date_format": DATETIME_FORMAT})],
                ),
                t.ColumnDefinition(
                    field="finished",
                    width=180,
                    visible=False,
                    formatters=[(t.formatters.DateFormatter, {"date_format": DATETIME_FORMAT})],
                ),
                t.ColumnDefinition(
                    field="duration",
                    width=110,
                    filterable=False,
                    formatters=[(cf.DurationFormatter, {})],
                ),
                t.ColumnDefinition(field="exit_code", title=tk._("Exit code"), width=100, horizontal_align="center"),
                t.ColumnDefinition(
                    field="stdout",
                    title=tk._("Output"),
                    min_width=180,
                    formatters=[(t.formatters.DialogModalFormatter, {"max_length": 3, "modal_title": tk._("Output")})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(
                    field="stderr",
                    title=tk._("Errors"),
                    min_width=180,
                    formatters=[(t.formatters.DialogModalFormatter, {"max_length": 3, "modal_title": tk._("Errors")})],
                    tabulator_formatter="html",
                ),
                t.ColumnDefinition(field="worker", width=160, visible=False),
            ],
            table_actions=[
                t.TableActionDefinition(
                    action="purge",
                    label=tk._("Purge runs older than {days} days").format(days=config.history_retention_days()),
                    icon="fa fa-broom",
                    attrs={"class": "text-danger"},
                    callback=self.table_action_purge,
                ),
            ],
            exporters=t.ALL_EXPORTERS,
        )

    def table_action_purge(self) -> t.ActionHandlerResult:
        result, error = _call_action("cron_run_purge", {})
        return _result(error, tk._("{n} runs have been deleted.").format(n=result["deleted"] if result else 0))
