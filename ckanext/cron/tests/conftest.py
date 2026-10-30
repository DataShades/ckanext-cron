from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from ckan.tests import factories
from ckan.tests.helpers import call_action

from ckanext.cron import config as cron_config
from ckanext.cron import pg_cron
from ckanext.cron.model import PG_CRON_PREFIX

UNREACHABLE_DATABASE_URL = "postgresql://nobody:wrong@localhost:1/none"


@pytest.fixture
def clean_db(reset_db: Any, migrate_db_for: Any) -> None:
    reset_db()
    migrate_db_for("cron")


@pytest.fixture(autouse=True)
def pg_cron_jobs(request: pytest.FixtureRequest, with_plugins: None) -> Iterator[None]:
    """Skip the test unless pg_cron is reachable, and remove its pg_cron jobs afterwards.

    On CI, where pg_cron is set up, the test fails instead of being skipped.
    Tests marked `without_pg_cron`, and tests that point
    `ckanext.cron.database_url` elsewhere with `ckan_config`, are left alone.
    """
    if request.node.get_closest_marker("without_pg_cron") or any(
        m.args[0] == cron_config.CONF_DATABASE_URL for m in request.node.iter_markers("ckan_config")
    ):
        yield
        return

    info = pg_cron.get_info()

    if not info["available"]:
        message = f"pg_cron is not available: {info['error']}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    yield
    remove_test_pg_cron_jobs()


def remove_test_pg_cron_jobs() -> None:
    """Unschedule the pg_cron jobs that enqueue runs in the test database."""
    job = pg_cron.job_table

    with pg_cron.connect(write=True) as conn:
        jobids = conn.execute(
            sa.select(job.c.jobid).where(
                job.c.jobname.startswith(PG_CRON_PREFIX), job.c.database == cron_config.ckan_database()
            )
        ).scalars()

        for jobid in list(jobids):
            conn.execute(sa.text("SELECT cron.unschedule(CAST(:jobid AS bigint))"), {"jobid": jobid})
            conn.execute(sa.delete(pg_cron.run_table).where(pg_cron.run_table.c.jobid == jobid))


@pytest.fixture
def sysadmin() -> dict[str, Any]:
    return factories.SysadminWithToken()


@pytest.fixture
def job_factory(sysadmin: dict[str, Any]) -> Callable[..., dict[str, Any]]:
    counter = iter(range(1, 1000))

    def factory(**kwargs: Any) -> dict[str, Any]:
        data = {
            "name": f"job {next(counter)}",
            "command": "jobs list",
            "schedule": "0 3 * * *",
            **kwargs,
        }
        return call_action("cron_job_create", {"user": sysadmin["name"]}, **data)

    return factory
