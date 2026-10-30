from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

import ckan.plugins.toolkit as tk
from ckan import model
from ckan.tests import factories
from ckan.tests.helpers import call_action, call_auth

from ckanext.cron import config as cron_config
from ckanext.cron import pg_cron
from ckanext.cron.config import CONF_DATABASE_URL
from ckanext.cron.model import CronJob, CronRun
from ckanext.cron.tests.conftest import UNREACHABLE_DATABASE_URL


def _pg_job(job_id: str) -> pg_cron.PgCronJob | None:
    return pg_cron.find_job(model.Session.get(CronJob, job_id).pg_cron_name)


@pytest.mark.usefixtures("clean_db")
class TestJobCreate:
    def test_create(self, job_factory, sysadmin):
        job = job_factory(name="Harvest", command="search-index rebuild", schedule="*/30 * * * *", active=False)

        assert job["name"] == "Harvest"
        assert job["command"] == "search-index rebuild"
        assert job["schedule"] == "*/30 * * * *"
        assert job["active"] is False
        assert job["last_run"] is None

        assert call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"]) == job

    def test_pg_cron_job(self, job_factory):
        job = job_factory(schedule="*/30 * * * *")
        pg_job = _pg_job(job["id"])

        assert pg_job["schedule"] == "*/30 * * * *"
        assert pg_job["database"] == tk.config["sqlalchemy.url"].rsplit("/", 1)[-1]
        assert pg_job["active"] is True

    def test_command_is_normalized(self, job_factory):
        job = job_factory(command="ckan  cron call-action  package_search 'q=title:\"a b\"'")

        assert job["command"] == "cron call-action package_search 'q=title:\"a b\"'"

    @pytest.mark.parametrize(
        ("command", "error"),
        [
            ("no-such-command", "Unknown ckan command"),
            ("search-index", "needs one of these subcommands"),
            ("search-index nope", "needs one of these subcommands"),
            ("-c /etc/ckan.ini jobs list", "config file"),
            ("jobs list --config=/etc/ckan.ini", "config file"),
            ("run", "can't run as a cron job"),
            ("cron worker", "can't run as a cron job"),
            ("jobs list 'unclosed", "quotation"),
        ],
    )
    def test_invalid_command(self, job_factory, command, error):
        with pytest.raises(tk.ValidationError) as e:
            job_factory(command=command)

        assert error in e.value.error_dict["command"][0]

    def test_duplicate_name(self, job_factory):
        job_factory(name="same")

        with pytest.raises(tk.ValidationError) as e:
            job_factory(name="same")

        assert "name" in e.value.error_dict

    def test_invalid_schedule_leaves_nothing_behind(self, job_factory):
        with pytest.raises(tk.ValidationError) as e:
            job_factory(name="broken", schedule="every tuesday")

        assert "invalid schedule" in e.value.error_dict["schedule"][0]
        assert model.Session.scalar(sa.select(sa.func.count()).select_from(CronJob)) == 0

    def test_list(self, job_factory, sysadmin):
        job_factory(name="b")
        job_factory(name="a")

        assert [job["name"] for job in call_action("cron_job_list", {"user": sysadmin["name"]})] == ["a", "b"]


@pytest.mark.usefixtures("clean_db")
class TestJobUpdate:
    def test_update(self, job_factory, sysadmin):
        job = job_factory()

        result = call_action(
            "cron_job_update",
            {"user": sysadmin["name"]},
            id=job["id"],
            name="renamed",
            command="search-index rebuild",
            schedule="@daily",
            active=False,
        )

        assert result["name"] == "renamed"
        assert result["command"] == "search-index rebuild"
        assert result["schedule"] == "@daily"
        assert result["active"] is False

    def test_partial_update_keeps_other_fields(self, job_factory, sysadmin):
        job = job_factory(schedule="0 4 * * *", command="jobs list")

        result = call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], active=False)

        assert result["schedule"] == "0 4 * * *"
        assert result["command"] == "jobs list"
        assert result["active"] is False

    def test_invalid_schedule_keeps_job(self, job_factory, sysadmin):
        job = job_factory()

        with pytest.raises(tk.ValidationError):
            call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], name="new", schedule="nope")

        assert call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])["name"] == job["name"]

    def test_name_taken_by_another_job(self, job_factory, sysadmin):
        job_factory(name="taken")
        job = job_factory()

        with pytest.raises(tk.ValidationError) as e:
            call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], name="taken")

        assert "name" in e.value.error_dict

    def test_keeping_own_name(self, job_factory, sysadmin):
        job = job_factory(name="mine")

        assert call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], name="mine")["name"] == "mine"

    def test_who_changed_it(self, job_factory, sysadmin):
        job = job_factory()
        other = factories.Sysadmin()

        updated = call_action("cron_job_update", {"user": other["name"]}, id=job["id"], active=False)

        assert job["created_by"] == job["modified_by"] == sysadmin["id"]
        assert updated["created_by"] == sysadmin["id"]
        assert updated["modified_by"] == other["id"]
        assert updated["modified"] > job["modified"]

    def test_allow_overlap(self, job_factory, sysadmin):
        job = job_factory()
        assert job["allow_overlap"] is False

        updated = call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], allow_overlap=True)

        assert updated["allow_overlap"] is True

    def test_update_refreshes_outdated_trigger_command(self, job_factory, sysadmin):
        job = job_factory()
        pg_cron.alter_job(_pg_job(job["id"])["jobid"], command="SELECT 1")

        call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], name="renamed")

        assert _pg_job(job["id"])["command"] == model.Session.get(CronJob, job["id"]).trigger_command()

    def test_reschedule_missing_pg_cron_job(self, job_factory, sysadmin):
        job = job_factory()
        pg_cron.unschedule_job(_pg_job(job["id"])["jobid"])

        assert call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])["schedule"] is None

        with pytest.raises(tk.ValidationError) as e:
            call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], active=False)
        assert "schedule" in e.value.error_dict

        result = call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], schedule="@hourly")
        assert result["schedule"] == "@hourly"


@pytest.mark.usefixtures("clean_db")
class TestJobDelete:
    def test_delete(self, job_factory, sysadmin):
        job = job_factory()
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])
        pg_cron_name = model.Session.get(CronJob, job["id"]).pg_cron_name

        assert call_action("cron_job_delete", {"user": sysadmin["name"]}, id=job["id"])

        assert pg_cron.find_job(pg_cron_name) is None
        assert model.Session.scalar(sa.select(sa.func.count()).select_from(CronRun)) == 0

        with pytest.raises(tk.ValidationError):
            call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])


@pytest.mark.usefixtures("clean_db")
class TestRuns:
    def test_trigger(self, job_factory, sysadmin):
        job = job_factory()

        run = call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])

        assert run["status"] == "queued"
        assert run["trigger"] == "manual"
        assert run["triggered_by"] == sysadmin["id"]
        assert call_action("cron_job_show", {"user": sysadmin["name"]}, id=job["id"])["last_run"]["id"] == run["id"]

    def test_trigger_while_unfinished(self, job_factory, sysadmin):
        job = job_factory()
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])

        with pytest.raises(tk.ValidationError) as e:
            call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])

        assert "already queued or running" in e.value.error_dict["id"][0]

    def test_trigger_with_overlap(self, job_factory, sysadmin):
        job = job_factory(allow_overlap=True)

        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])

        assert len(call_action("cron_run_list", {"user": sysadmin["name"]}, job_id=job["id"])) == 2

    @pytest.mark.parametrize(("allow_overlap", "runs"), [(False, 1), (True, 2)])
    def test_scheduled_trigger_skips_while_unfinished(self, job_factory, allow_overlap, runs):
        """The SQL that pg_cron runs on schedule."""
        job = model.Session.get(CronJob, job_factory(allow_overlap=allow_overlap)["id"])

        for _ in range(2):
            model.Session.execute(sa.text(job.trigger_command()))
        model.Session.commit()

        assert model.Session.scalar(sa.select(sa.func.count()).where(CronRun.job_id == job.id)) == runs

    def test_scheduled_trigger_of_missing_job(self, job_factory):
        job = model.Session.get(CronJob, job_factory()["id"])
        command = job.trigger_command()
        model.Session.execute(sa.delete(CronJob))
        model.Session.commit()

        model.Session.execute(sa.text(command))
        model.Session.commit()

        assert model.Session.scalar(sa.select(sa.func.count()).select_from(CronRun)) == 0

    def test_run_list(self, job_factory, sysadmin):
        job = job_factory(allow_overlap=True)
        other = job_factory()
        first = call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])
        second = call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])
        call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=other["id"])

        runs = call_action("cron_run_list", {"user": sysadmin["name"]}, job_id=job["id"])

        assert [run["id"] for run in runs] == [second["id"], first["id"]]
        assert call_action("cron_run_show", {"user": sysadmin["name"]}, id=first["id"]) == first

    def test_run_list_unknown_status(self, sysadmin):
        with pytest.raises(tk.ValidationError) as e:
            call_action("cron_run_list", {"user": sysadmin["name"]}, status="done")

        assert "status" in e.value.error_dict

    def test_purge(self, job_factory, sysadmin):
        job = job_factory()
        old_finished = CronRun(job_id=job["id"], status="succeeded", created=datetime.now(timezone.utc) - timedelta(days=40))
        old_queued = CronRun(job_id=job["id"], status="queued", created=datetime.now(timezone.utc) - timedelta(days=40))
        recent = CronRun(job_id=job["id"], status="failed")
        model.Session.add_all([old_finished, old_queued, recent])
        model.Session.commit()

        assert call_action("cron_run_purge", {"user": sysadmin["name"]}) == {"deleted": 1, "pg_cron_deleted": 0}
        assert {run["id"] for run in call_action("cron_run_list", {"user": sysadmin["name"]})} == {
            old_queued.id,
            recent.id,
        }


def _log_pg_cron_run(job_id: str, status: str, age: timedelta, message: str = "") -> None:
    """Add a run to pg_cron's log, as pg_cron does when the schedule fires."""
    pg_job = _pg_job(job_id)

    with pg_cron.connect(write=True) as conn:
        conn.execute(
            sa.insert(pg_cron.run_table).values(
                runid=sa.text("nextval('cron.runid_seq')"),
                jobid=pg_job["jobid"],
                status=status,
                return_message=message,
                start_time=datetime.now(timezone.utc) - age,
            )
        )


def _pg_cron_log(job_id: str) -> list[str]:
    run = pg_cron.run_table

    with pg_cron.connect() as conn:
        stmt = sa.select(run.c.status).where(run.c.jobid == _pg_job(job_id)["jobid"]).order_by(run.c.runid)
        return list(conn.execute(stmt).scalars())


@pytest.mark.usefixtures("clean_db")
class TestPgCronLog:
    def test_failed_trigger_is_reported(self, job_factory, sysadmin):
        job = job_factory()
        other = job_factory()
        _log_pg_cron_run(job["id"], "succeeded", timedelta(minutes=2))
        _log_pg_cron_run(job["id"], "failed", timedelta(minutes=1), "permission denied for table cron_run")
        _log_pg_cron_run(other["id"], "failed", timedelta(minutes=2), "old failure")
        _log_pg_cron_run(other["id"], "succeeded", timedelta(minutes=1))

        jobs = {job["id"]: job for job in call_action("cron_job_list", {"user": sysadmin["name"]})}

        assert jobs[job["id"]]["trigger_error"] == "permission denied for table cron_run"
        assert jobs[other["id"]]["trigger_error"] is None

    def test_purge(self, job_factory, sysadmin):
        job = job_factory()
        other = job_factory()
        _log_pg_cron_run(job["id"], "succeeded", timedelta(days=40))
        _log_pg_cron_run(job["id"], "failed", timedelta(days=1))
        _log_pg_cron_run(other["id"], "succeeded", timedelta(days=40))

        result = call_action("cron_run_purge", {"user": sysadmin["name"]}, job_id=job["id"])

        assert result["pg_cron_deleted"] == 1
        assert _pg_cron_log(job["id"]) == ["failed"]
        assert _pg_cron_log(other["id"]) == ["succeeded"]

        assert call_action("cron_run_purge", {"user": sysadmin["name"]})["pg_cron_deleted"] == 1
        assert _pg_cron_log(other["id"]) == []

    def test_purge_without_pg_cron(self, job_factory, sysadmin, monkeypatch):
        def unavailable(*args):
            raise pg_cron.PgCronError("connection refused")

        job_factory()
        monkeypatch.setattr(pg_cron, "purge_run_details", unavailable)

        assert call_action("cron_run_purge", {"user": sysadmin["name"]}) == {"deleted": 0, "pg_cron_deleted": None}


@pytest.mark.usefixtures("clean_db")
class TestAuth:
    @pytest.mark.parametrize(
        "action",
        [
            "cron_manage",
            "cron_info",
            "cron_job_list",
            "cron_job_show",
            "cron_job_create",
            "cron_job_update",
            "cron_job_delete",
            "cron_job_trigger",
            "cron_worker_list",
            "cron_run_list",
            "cron_run_show",
            "cron_run_purge",
        ],
    )
    def test_regular_user_is_denied(self, action):
        user = factories.User()

        with pytest.raises(tk.NotAuthorized):
            call_auth(action, {"user": user["name"]})

    def test_sysadmin_is_allowed(self, sysadmin):
        assert call_auth("cron_job_create", {"user": sysadmin["name"]})


class TestInfo:
    def test_info(self):
        info = pg_cron.get_info()

        assert info["available"]
        assert info["version"]

    @pytest.mark.ckan_config(CONF_DATABASE_URL, UNREACHABLE_DATABASE_URL)
    def test_unreachable(self):
        info = pg_cron.get_info()

        assert not info["available"]
        assert info["error"]

    def test_ckan_database_on_another_server(self, monkeypatch):
        monkeypatch.setattr(cron_config, "ckan_database", lambda: "no_such_ckan_database")

        info = pg_cron.get_info()

        assert not info["available"]
        assert "no_such_ckan_database" in info["error"]


LIMITED_ROLE = "ckanext_cron_test_limited"


@pytest.fixture
def limited_role_url():
    """URL of a pg_cron connection as a role that isn't a superuser, like most deployments.

    The role has USAGE on the cron schema but not the grants from
    `pg_cron.grant_statement`. It is created on first use and kept, so pooled
    connections never outlive it.
    """
    if not pg_cron.get_info()["is_superuser"]:
        pytest.skip("creating a test role needs a superuser connection")

    with pg_cron.connect(write=True) as conn:
        if not conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": LIMITED_ROLE}).scalar():
            conn.execute(sa.text(f"CREATE ROLE {LIMITED_ROLE} LOGIN PASSWORD 'pass'"))
        conn.execute(sa.text(f"GRANT USAGE ON SCHEMA cron TO {LIMITED_ROLE}"))
        conn.execute(sa.text(pg_cron.grant_statement(LIMITED_ROLE).replace("GRANT", "REVOKE", 1).replace(" TO ", " FROM ")))

    url = make_url(tk.config[CONF_DATABASE_URL]).set(username=LIMITED_ROLE, password="pass")

    yield url.render_as_string(hide_password=False)

    with pg_cron.connect(write=True) as conn:
        conn.execute(sa.text("SELECT cron.unschedule(jobid) FROM cron.job WHERE username = :role"), {"role": LIMITED_ROLE})


@pytest.fixture
def as_limited_role(limited_role_url, monkeypatch):
    monkeypatch.setitem(tk.config, CONF_DATABASE_URL, limited_role_url)


@pytest.fixture
def with_grants(limited_role_url):
    with pg_cron.connect(write=True) as conn:
        conn.execute(sa.text(pg_cron.grant_statement(LIMITED_ROLE)))


@pytest.mark.usefixtures("clean_db", "as_limited_role")
class TestLimitedRole:
    def test_info(self):
        info = pg_cron.get_info()

        assert info["available"], info["error"]
        assert info["is_superuser"] is False
        assert info["timezone"] is None
        assert info["missing_privileges"] == [
            "cron.schedule_in_database(text,text,text,text,text,boolean)",
            "cron.alter_job(bigint,text,text,text,text,boolean)",
        ]

    def test_create_without_grants(self, job_factory):
        with pytest.raises(tk.ValidationError) as e:
            job_factory()

        assert "GRANT EXECUTE" in e.value.error_dict["message"][0]

    @pytest.mark.usefixtures("with_grants")
    def test_manage_jobs_with_grants(self, job_factory, sysadmin):
        assert pg_cron.get_info()["missing_privileges"] == []

        job = job_factory()
        assert _pg_job(job["id"])["username"] == LIMITED_ROLE

        updated = call_action("cron_job_update", {"user": sysadmin["name"]}, id=job["id"], active=False)
        assert updated["active"] is False

        assert call_action("cron_job_delete", {"user": sysadmin["name"]}, id=job["id"])
