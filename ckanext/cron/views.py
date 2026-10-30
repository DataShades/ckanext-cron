from __future__ import annotations

from typing import Any

from flask import Blueprint, Response

import ckan.plugins.toolkit as tk

import ckanext.tables.shared as t

from ckanext.cron import jobs, pg_cron
from ckanext.cron.tables import CronJobTable, CronRunTable

cron = Blueprint("cron", __name__, url_prefix="/ckan-admin/cron")

FORM_FIELDS = ("name", "command", "schedule")


def N_(message: str) -> str:
    """Mark a message for translation without translating it yet, as the request locale isn't known."""
    return message


class CronTableView(t.GenericTableView):
    """Table page that explains what's wrong instead of failing when pg_cron is unusable.

    `breadcrumb_label` and `page_title` are untranslated, see N_.
    """

    def _render_full_page(self, table: t.TableDefinition, **extra_vars: Any) -> str:
        info = pg_cron.get_info()

        if not info["available"]:
            return tk.render("cron/unavailable.html", extra_vars={"info": info})

        return table.render_table(
            breadcrumb_label=tk._(self.breadcrumb_label),
            page_title=tk._(self.page_title),
            info=info,
            workers=jobs.live_workers(),
            delayed_runs=jobs.delayed_runs(),
            queue_delay_minutes=int(jobs.QUEUE_DELAY_WARNING.total_seconds() // 60),
            **(extra_vars or self.extra_vars()),
        )

    def extra_vars(self) -> dict[str, Any]:
        return {}


class CronJobsView(CronTableView):
    """Job list, plus the form to create a job or edit the one named by `?edit=<id>`."""

    def extra_vars(self) -> dict[str, Any]:
        edit_id = tk.request.args.get("edit")

        if not edit_id:
            return {}

        try:
            job = tk.get_action("cron_job_show")({"user": tk.current_user.name}, {"id": edit_id})
        except tk.ValidationError:
            tk.h.flash_error(tk._("Cron job not found"))
            return {}

        return {"form_data": job, "is_edit": True}

    def post(self) -> str | Response:
        form_action = tk.request.form.get("form_action")

        if form_action not in ("create", "update"):
            return super().post()

        if not self.check_access():
            return tk.abort(403, tk._("You are not authorized to perform this action."))

        return self._submit_form(form_action)

    def _submit_form(self, form_action: str) -> str | Response:
        form = tk.request.form
        data_dict: dict[str, Any] = {key: form.get(key, "") for key in FORM_FIELDS}
        data_dict["active"] = "active" in form
        data_dict["allow_overlap"] = "allow_overlap" in form

        if form_action == "update":
            data_dict["id"] = form.get("id", "")

        try:
            job = tk.get_action(f"cron_job_{form_action}")({"user": tk.current_user.name}, data_dict)
        except tk.ValidationError as e:
            # show the form again so the user doesn't lose what they typed
            tk.h.flash_error(tk._("The job has not been saved, see the errors below."))

            return self._render_full_page(
                self.table(),  # type: ignore[call-arg]
                form_data=data_dict,
                form_errors=e.error_dict,
                is_edit=form_action == "update",
            )

        if form_action == "create":
            tk.h.flash_success(tk._('The job "{name}" has been created.').format(name=job["name"]))
        else:
            tk.h.flash_success(tk._('The job "{name}" has been updated.').format(name=job["name"]))

        return tk.redirect_to("cron.jobs")


cron.add_url_rule(
    "/",
    view_func=CronJobsView.as_view(
        "jobs",
        table=CronJobTable,
        breadcrumb_label=N_("Cron jobs"),
        page_title=N_("Cron jobs"),
    ),
)
cron.add_url_rule(
    "/runs",
    view_func=CronTableView.as_view(
        "runs",
        table=CronRunTable,
        breadcrumb_label=N_("Run history"),
        page_title=N_("Run history"),
    ),
)
