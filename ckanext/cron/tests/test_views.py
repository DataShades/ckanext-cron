from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import ckan.plugins.toolkit as tk
from ckan import model
from ckan.tests import factories
from ckan.tests.helpers import call_action

from ckanext.cron.config import CONF_DATABASE_URL
from ckanext.cron.model import CronJob, CronRun, CronWorker
from ckanext.cron.tests.conftest import UNREACHABLE_DATABASE_URL

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _auth(user):
    return {"Authorization": user["token"]}


def _job_by_name(name: str) -> CronJob | None:
    model.Session.expire_all()
    return model.Session.query(CronJob).filter_by(name=name).one_or_none()


@pytest.mark.usefixtures("clean_db")
class TestAccess:
    @pytest.mark.parametrize("endpoint", ["cron.jobs", "cron.runs"])
    def test_anonymous(self, app, endpoint):
        app.get(tk.url_for(endpoint), status=403)

    @pytest.mark.parametrize("endpoint", ["cron.jobs", "cron.runs"])
    def test_regular_user(self, app, endpoint):
        user = factories.UserWithToken()
        app.get(tk.url_for(endpoint), headers=_auth(user), status=403)

    def test_regular_user_cannot_create(self, app):
        user = factories.UserWithToken()
        app.post(
            tk.url_for("cron.jobs"),
            data=_form("create", name="x", schedule="@daily"),
            headers=_auth(user),
            status=403,
        )

        assert _job_by_name("x") is None


@pytest.mark.usefixtures("clean_db")
class TestJobsPage:
    def test_page(self, app, sysadmin):
        resp = app.get(tk.url_for("cron.jobs"), headers=_auth(sysadmin))

        assert "New job" in resp.body
        assert 'name="schedule"' in resp.body
        assert '<option value="search-index rebuild">' in resp.body

    def test_ajax_data(self, app, sysadmin, job_factory):
        job = job_factory(name="Harvest", schedule="*/30 * * * *")
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])

        resp = app.get(tk.url_for("cron.jobs"), headers={**AJAX, **_auth(sysadmin)})
        row = resp.json["data"][0]

        assert row["name"] == "Harvest"
        assert "Every 30 minutes" in row["schedule"]
        assert "queued" in row["last_status"]

    def test_create_job(self, app, sysadmin):
        resp = app.post(
            tk.url_for("cron.jobs"),
            data=_form("create", name="Harvest", command="ckan search-index rebuild", schedule="*/30 * * * *", active="on"),
            headers=_auth(sysadmin),
            follow_redirects=False,
        )

        assert resp.status_code == 302
        job = _job_by_name("Harvest")
        assert job.command == "search-index rebuild"
        assert job.allow_overlap is False

    def test_create_job_with_overlap(self, app, sysadmin):
        data = _form("create", name="Overlap", command="jobs list", schedule="@hourly", active="on", allow_overlap="on")

        app.post(tk.url_for("cron.jobs"), data=data, headers=_auth(sysadmin), follow_redirects=False)

        assert _job_by_name("Overlap").allow_overlap is True

    def test_create_invalid_keeps_input(self, app, sysadmin):
        resp = app.post(
            tk.url_for("cron.jobs"),
            data=_form("create", name="Broken", command="jobs list --queue keep-me", schedule="whenever"),
            headers=_auth(sysadmin),
        )

        assert resp.status_code == 200
        assert "invalid schedule" in resp.body
        assert "jobs list --queue keep-me" in resp.body
        assert _job_by_name("Broken") is None

    def test_create_invalid_command(self, app, sysadmin):
        resp = app.post(
            tk.url_for("cron.jobs"),
            data=_form("create", name="Broken", command="no-such-command", schedule="@daily"),
            headers=_auth(sysadmin),
        )

        assert "Unknown ckan command" in resp.body
        assert 'value="no-such-command"' in resp.body
        assert _job_by_name("Broken") is None

    def test_edit_form(self, app, sysadmin, job_factory):
        job = job_factory(name="Editable", command="cron call-action status_show")

        resp = app.get(tk.url_for("cron.jobs", edit=job["id"]), headers=_auth(sysadmin))

        assert "Edit job Editable" in resp.body
        assert 'value="cron call-action status_show"' in resp.body
        assert f'href="/user/{sysadmin["name"]}"' in resp.body

    def test_edit_missing_job(self, app, sysadmin):
        resp = app.get(tk.url_for("cron.jobs", edit="missing"), headers=_auth(sysadmin))

        assert "Cron job not found" in resp.body
        assert "Edit job" not in resp.body

    def test_update(self, app, sysadmin, job_factory):
        job = job_factory(name="Old")

        app.post(
            tk.url_for("cron.jobs"),
            data=_form("update", id=job["id"], name="New", command="search-index rebuild", schedule="@weekly"),
            headers=_auth(sysadmin),
        )

        updated = call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])
        assert updated["name"] == "New"
        assert updated["schedule"] == "@weekly"
        assert updated["command"] == "search-index rebuild"
        assert updated["active"] is False

    def test_row_action_run_now(self, app, sysadmin, job_factory):
        job = job_factory()

        resp = app.post(
            tk.url_for("cron.jobs"),
            data={"row_action": "run_now", "row": json.dumps({"id": job["id"]})},
            headers=_auth(sysadmin),
        )

        assert resp.json["success"], resp.json
        assert model.Session.query(CronRun).filter_by(job_id=job["id"]).one().trigger == "manual"

    def test_row_action_toggle(self, app, sysadmin, job_factory):
        job = job_factory()

        resp = app.post(
            tk.url_for("cron.jobs"),
            data={"row_action": "toggle_active", "row": json.dumps({"id": job["id"]})},
            headers=_auth(sysadmin),
        )

        assert resp.json["success"], resp.json
        assert call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])["active"] is False

    def test_row_action_delete(self, app, sysadmin, job_factory):
        job = job_factory(name="Doomed")

        resp = app.post(
            tk.url_for("cron.jobs"),
            data={"row_action": "delete", "row": json.dumps({"id": job["id"]})},
            headers=_auth(sysadmin),
        )

        assert resp.json["success"], resp.json
        assert _job_by_name("Doomed") is None

    def test_row_action_runs_redirect(self, app, sysadmin, job_factory):
        job = job_factory()

        resp = app.post(
            tk.url_for("cron.jobs"),
            data={"row_action": "runs", "row": json.dumps({"id": job["id"]})},
            headers=_auth(sysadmin),
        )

        assert f"value-cron_runs={job['id']}" in resp.json["redirect"]

    def test_bulk_disable(self, app, sysadmin, job_factory):
        jobs = [job_factory(), job_factory()]

        resp = app.post(
            tk.url_for("cron.jobs"),
            data={"bulk_action": "disable", "rows": json.dumps([{"id": j["id"], "name": j["name"]} for j in jobs])},
            headers=_auth(sysadmin),
        )

        assert resp.json["success"], resp.json
        assert all(call_action("cron_job_show", {"user": sysadmin["name"]}, id=j["id"])["active"] is False for j in jobs)

    def test_no_worker_warning(self, app, sysadmin):
        model.Session.add(CronWorker(name="dead:1", last_seen=datetime.now(timezone.utc) - timedelta(minutes=5)))
        model.Session.commit()

        resp = app.get(tk.url_for("cron.jobs"), headers=_auth(sysadmin))

        assert "No cron worker is running" in resp.body
        assert "dead:1" not in resp.body

    def test_workers_and_delayed_runs(self, app, sysadmin, job_factory):
        job = job_factory()
        run = CronRun(job_id=job["id"], status="running")
        model.Session.add_all([run, CronRun(job_id=job["id"], created=datetime.now(timezone.utc) - timedelta(hours=1))])
        model.Session.flush()
        model.Session.add_all([CronWorker(name="busy:1", run_id=run.id), CronWorker(name="idle:2")])
        model.Session.commit()

        resp = app.get(tk.url_for("cron.jobs"), headers=_auth(sysadmin))

        assert "No cron worker is running" not in resp.body
        assert f"busy:1</code> (running run {run.id})" in resp.body
        assert "idle:2</code> (waiting)" in resp.body
        assert "for more than 5 minutes" in resp.body

    @pytest.mark.ckan_config(CONF_DATABASE_URL, UNREACHABLE_DATABASE_URL)
    def test_unavailable(self, app, sysadmin):
        resp = app.get(tk.url_for("cron.jobs"), headers=_auth(sysadmin))

        assert "pg_cron is not available" in resp.body
        assert "shared_preload_libraries" in resp.body


@pytest.mark.usefixtures("clean_db")
class TestRunsPage:
    def test_page(self, app, sysadmin):
        resp = app.get(tk.url_for("cron.runs"), headers=_auth(sysadmin))

        assert "Run history" in resp.body

    def test_ajax_data(self, app, sysadmin, job_factory):
        job = job_factory(name="Listed")
        other = job_factory()
        run = call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=other["id"])
        params = {"filters": json.dumps([{"field": "job_id", "operator": "=", "value": job["id"]}])}

        resp = app.get(tk.url_for("cron.runs", **params), headers={**AJAX, **_auth(sysadmin)})
        rows = resp.json["data"]

        assert [row["id"] for row in rows] == [run["id"]]
        assert rows[0]["job_name"] == "Listed"
        assert "queued" in rows[0]["status"]
        assert rows[0]["triggered_by"] == sysadmin["name"]

    def test_output_and_errors_open_in_modal(self, app, sysadmin, job_factory):
        job = job_factory()
        model.Session.add(CronRun(job_id=job["id"], status="failed", exit_code=1, stdout="a\nlonger\noutput", stderr="Boom!"))
        model.Session.commit()

        resp = app.get(tk.url_for("cron.runs"), headers={**AJAX, **_auth(sysadmin)})
        row = resp.json["data"][0]

        assert "showModal()" in row["stdout"]
        assert "showModal()" in row["stderr"]

    def test_purge(self, app, sysadmin):
        resp = app.post(tk.url_for("cron.runs"), data={"table_action": "purge"}, headers=_auth(sysadmin))

        assert resp.json["success"], resp.json


def _form(submit: str, **fields: str) -> dict[str, str]:
    return {"form_action": submit, "command": "jobs list", **fields}
