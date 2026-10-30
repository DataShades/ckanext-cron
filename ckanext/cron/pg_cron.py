"""Access to the pg_cron extension.

Jobs are changed only through pg_cron's own functions, which check permissions
and refresh the scheduler.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TypedDict

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ckanext.cron import config

log = logging.getLogger(__name__)

metadata = sa.MetaData(schema="cron")

job_table = sa.Table(
    "job",
    metadata,
    sa.Column("jobid", sa.BigInteger, primary_key=True),
    sa.Column("jobname", sa.Text),
    sa.Column("schedule", sa.Text),
    sa.Column("command", sa.Text),
    sa.Column("database", sa.Text),
    sa.Column("username", sa.Text),
    sa.Column("active", sa.Boolean),
)

run_table = sa.Table(
    "job_run_details",
    metadata,
    sa.Column("runid", sa.BigInteger, primary_key=True),
    sa.Column("jobid", sa.BigInteger),
    sa.Column("status", sa.Text),
    sa.Column("return_message", sa.Text),
    sa.Column("start_time", sa.DateTime(timezone=True)),
)

# pg_cron functions this extension calls. pg_cron lets ordinary roles execute
# only the last one unless a superuser grants the others.
REQUIRED_FUNCTIONS = [
    "cron.schedule_in_database(text,text,text,text,text,boolean)",
    "cron.alter_job(bigint,text,text,text,text,boolean)",
    "cron.unschedule(bigint)",
]

_engines: dict[str, Engine] = {}


class PgCronError(Exception):
    """pg_cron is unreachable or rejected the request."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class PgCronJob(TypedDict):
    jobid: int
    jobname: str
    schedule: str
    command: str
    active: bool
    database: str
    username: str


class PgCronInfo(TypedDict):
    available: bool
    error: str | None
    version: str | None
    # None when the role isn't allowed to read cron.timezone
    timezone: str | None
    database: str | None
    username: str | None
    is_superuser: bool
    missing_privileges: list[str]


def get_engine() -> Engine:
    url = config.database_url()

    if url not in _engines:
        _engines[url] = sa.create_engine(url, pool_pre_ping=True)

    return _engines[url]


def _wrap_error(err: SQLAlchemyError) -> PgCronError:
    """Turn a DB error into a PgCronError with PostgreSQL's own message."""
    orig = getattr(err, "orig", None)
    diag = getattr(orig, "diag", None)

    if diag is not None and diag.message_primary:
        return PgCronError(diag.message_primary, diag.message_hint)

    message = str(orig or err).strip().splitlines()
    return PgCronError(message[0] if message else type(err).__name__)


@contextmanager
def connect(write: bool = False) -> Iterator[sa.Connection]:
    """Open a connection to the pg_cron database, in a transaction if `write` is set.

    Database errors come out as PgCronError.
    """
    engine = get_engine()

    try:
        with engine.begin() if write else engine.connect() as conn:
            yield conn
    except SQLAlchemyError as e:
        raise _wrap_error(e) from e


def get_info() -> PgCronInfo:
    """Report whether pg_cron is usable through the configured connection."""
    info = PgCronInfo(
        available=False,
        error=None,
        version=None,
        timezone=None,
        database=None,
        username=None,
        is_superuser=False,
        missing_privileges=[],
    )
    # one query, so that rendering a page opens a single connection. pg_settings
    # hides cron.timezone from roles that may not read it, where current_setting fails.
    stmt = sa.text(
        "SELECT current_database() AS database, current_user AS username,"
        " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,"
        " (SELECT extversion FROM pg_extension WHERE extname = 'pg_cron') AS version,"
        " EXISTS (SELECT 1 FROM pg_database WHERE datname = :ckan_database) AS has_ckan_database,"
        " (SELECT setting FROM pg_settings WHERE name = 'cron.timezone') AS timezone,"
        # to_regprocedure gives NULL, not an error, for a function this pg_cron version lacks
        " ARRAY(SELECT fn FROM unnest(CAST(:functions AS text[])) WITH ORDINALITY AS f(fn, i)"
        "  WHERE NOT coalesce(has_function_privilege(to_regprocedure(fn), 'EXECUTE'), false)"
        "  ORDER BY i) AS missing_privileges"
    )
    params = {"ckan_database": config.ckan_database(), "functions": REQUIRED_FUNCTIONS}

    try:
        with connect() as conn:
            row = conn.execute(stmt, params).mappings().one()
    except PgCronError as e:
        info["error"] = str(e)
        return info

    info.update(
        database=row["database"],
        username=row["username"],
        is_superuser=bool(row["is_superuser"]),
        version=row["version"],
    )

    if not row["version"]:
        info["error"] = f'The pg_cron extension is not installed in the "{row["database"]}" database'
        return info

    if not row["has_ckan_database"]:
        info["error"] = f'The CKAN database "{config.ckan_database()}" is not on the PostgreSQL server where pg_cron runs'
        return info

    info["available"] = True
    # None when this role may not read cron.timezone; pg_cron uses GMT if it's empty
    info["timezone"] = None if row["timezone"] is None else row["timezone"] or "GMT"
    info["missing_privileges"] = list(row["missing_privileges"])

    return info


def grant_statement(username: str, functions: list[str] | None = None) -> str:
    """SQL that a superuser runs to let `username` manage its jobs from CKAN."""
    return f"GRANT EXECUTE ON FUNCTION {', '.join(functions or REQUIRED_FUNCTIONS)} TO {username};"


def _dictize_job(row: Any) -> PgCronJob:
    return PgCronJob(
        jobid=row["jobid"],
        jobname=row["jobname"],
        schedule=row["schedule"],
        command=row["command"],
        active=row["active"],
        database=row["database"],
        username=row["username"],
    )


def find_jobs(jobnames: list[str]) -> dict[str, PgCronJob]:
    """Return the pg_cron jobs with the given names, keyed by name."""
    if not jobnames:
        return {}

    stmt = sa.select(job_table).where(job_table.c.jobname.in_(jobnames))

    with connect() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {row["jobname"]: _dictize_job(row) for row in rows}


def find_job(jobname: str) -> PgCronJob | None:
    return find_jobs([jobname]).get(jobname)


def find_jobs_by_prefix(prefix: str, database: str) -> list[PgCronJob]:
    """Return the pg_cron jobs whose name starts with `prefix` and that run in `database`."""
    stmt = (
        sa.select(job_table)
        .where(job_table.c.jobname.startswith(prefix, autoescape=True), job_table.c.database == database)
        .order_by(job_table.c.jobid)
    )

    with connect() as conn:
        return [_dictize_job(row) for row in conn.execute(stmt).mappings()]


def schedule_job(jobname: str, schedule: str, command: str, database: str, active: bool = True) -> int:
    """Create a pg_cron job, owned by the connected role, and return its id."""
    stmt = sa.text("SELECT cron.schedule_in_database(:jobname, :schedule, :command, :database, NULL, :active)")
    params = {"jobname": jobname, "schedule": schedule, "command": command, "database": database, "active": active}

    with connect(write=True) as conn:
        return conn.execute(stmt, params).scalar_one()


def alter_job(jobid: int, schedule: str | None = None, command: str | None = None, active: bool | None = None) -> None:
    """Change the schedule, the command or the active flag of a job. None keeps the current value."""
    stmt = sa.text("SELECT cron.alter_job(job_id := :jobid, schedule := :schedule, command := :command, active := :active)")

    with connect(write=True) as conn:
        conn.execute(stmt, {"jobid": jobid, "schedule": schedule, "command": command, "active": active})


def unschedule_job(jobid: int) -> None:
    with connect(write=True) as conn:
        conn.execute(sa.text("SELECT cron.unschedule(CAST(:jobid AS bigint))"), {"jobid": jobid})


def failed_triggers(jobids: list[int]) -> dict[int, str]:
    """Return the error of each job whose latest pg_cron run failed, keyed by pg_cron job id.

    A failure here means pg_cron could not enqueue the run at all, so it never
    shows up in the CKAN run history.
    """
    if not jobids:
        return {}

    # job_run_details has no index on jobid, so look up the latest run of each
    # job by scanning the primary key backwards, which stops at the first match
    ids = sa.func.unnest(sa.cast(jobids, ARRAY(sa.BigInteger))).table_valued("jobid").render_derived("ids")
    latest = (
        sa.select(run_table.c.status, run_table.c.return_message)
        .where(run_table.c.jobid == ids.c.jobid)
        .order_by(run_table.c.runid.desc())
        .limit(1)
        .lateral("latest")
    )
    stmt = sa.select(ids.c.jobid, latest.c.return_message).join(latest, sa.true()).where(latest.c.status == "failed")

    with connect() as conn:
        return {row.jobid: row.return_message for row in conn.execute(stmt)}


def purge_run_details(jobids: list[int], older_than: datetime) -> int:
    """Delete pg_cron's log of the runs of these jobs that started before `older_than`. Return how many.

    pg_cron keeps a row for every run and never deletes them. A role may delete
    the rows of its own jobs without extra grants.
    """
    if not jobids:
        return 0

    stmt = sa.delete(run_table).where(run_table.c.jobid.in_(jobids), run_table.c.start_time < older_than)

    with connect(write=True) as conn:
        return conn.execute(stmt).rowcount
