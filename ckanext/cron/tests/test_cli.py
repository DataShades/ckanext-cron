from __future__ import annotations

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from ckan import model

from ckanext.cron import cli, jobs, pg_cron
from ckanext.cron.config import CONF_DATABASE_URL
from ckanext.cron.model import CronJob
from ckanext.cron.tests.conftest import UNREACHABLE_DATABASE_URL


def _check(*args):
    # invoked directly, because `ckan -c` would load the config file again and lose ckan_config
    return CliRunner().invoke(cli.check, list(args))


def _pg_job(job_id: str) -> pg_cron.PgCronJob | None:
    return pg_cron.find_job(model.Session.get(CronJob, job_id).pg_cron_name)


@pytest.mark.usefixtures("clean_db")
class TestCheck:
    def test_all_good(self, job_factory):
        job_factory()

        result = _check()

        assert result.exit_code == 0, result.output
        assert "1 jobs, all scheduled" in result.output

    def test_orphaned_pg_cron_job(self, job_factory):
        job = job_factory()
        model.Session.execute(sa.delete(CronJob))
        model.Session.commit()

        result = _check()

        assert result.exit_code == 1
        assert "belongs to a job that no longer exists" in result.output
        assert len(jobs.orphaned_pg_cron_jobs()) == 1

        result = _check("--fix")

        assert result.exit_code == 0, result.output
        assert "Unscheduled leftover pg_cron job" in result.output
        assert jobs.orphaned_pg_cron_jobs() == []
        assert not pg_cron.find_job(f"ckanext-cron-{job['id']}")

    def test_outdated_command(self, job_factory):
        job = job_factory()
        pg_cron.alter_job(_pg_job(job["id"])["jobid"], command="SELECT 1")

        assert _check().exit_code == 1

        assert _check("--fix").exit_code == 0
        assert _pg_job(job["id"])["command"] == model.Session.get(CronJob, job["id"]).trigger_command()

    def test_missing_pg_cron_job(self, job_factory):
        job = job_factory()
        pg_cron.unschedule_job(_pg_job(job["id"])["jobid"])

        result = _check("--fix")

        assert result.exit_code == 1
        assert "has no pg_cron job" in result.output

    @pytest.mark.ckan_config(CONF_DATABASE_URL, UNREACHABLE_DATABASE_URL)
    def test_pg_cron_unavailable(self):
        result = _check()

        assert result.exit_code == 1
        assert "pg_cron is not available" in result.output


@pytest.mark.usefixtures("clean_db")
def test_downgrade_unschedules_pg_cron_jobs(job_factory, migrate_db_for):
    job = job_factory()
    model.Session.remove()

    migrate_db_for("cron", "base", False)

    assert not pg_cron.find_job(f"ckanext-cron-{job['id']}")
