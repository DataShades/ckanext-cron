from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

import ckan.plugins.toolkit as tk
from ckan import model
from ckan.tests.helpers import call_action

from ckanext.cron import worker as worker_module
from ckanext.cron.model import CronRun, CronWorker
from ckanext.cron.worker import Worker, claim_run


@pytest.fixture
def worker():
    worker = Worker(tk.config["__file__"], poll_interval=10, timeout=60)
    yield worker
    worker._close_listener()


@pytest.fixture
def fake_ckan(tmp_path, monkeypatch):
    """Make the worker run a shell script instead of ckan. Return a function that sets the script body."""
    script = tmp_path / "ckan"
    monkeypatch.setitem(tk.config, "ckanext.cron.worker.executable", str(script))

    def write(body: str) -> None:
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    return write


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _trigger(sysadmin, job) -> int:
    return call_action("cron_job_trigger", {"user": sysadmin["name"]}, id=job["id"])["id"]


def _run(run_id: int) -> CronRun:
    model.Session.expire_all()
    return model.Session.get(CronRun, run_id)


@pytest.mark.usefixtures("clean_db")
class TestWorker:
    def test_success(self, worker, job_factory, sysadmin):
        run_id = _trigger(sysadmin, job_factory(command="cron call-action status_show"))

        assert worker.process_queue() == 1

        run = _run(run_id)
        assert run.status == "succeeded", run.stderr
        assert run.exit_code == 0
        assert json.loads(run.stdout)["site_title"]
        assert run.started <= run.finished
        assert run.worker == worker.name

    def test_failure(self, worker, job_factory, sysadmin):
        run_id = _trigger(sysadmin, job_factory(command="cron call-action user_show id=ckan-cron-missing-user"))

        worker.process_queue()

        run = _run(run_id)
        assert run.status == "failed"
        assert run.exit_code == 1
        assert "NotFound" in run.stderr

    def test_timeout_kills_the_command_and_its_children(self, worker, job_factory, sysadmin, fake_ckan, tmp_path):
        pid_file = tmp_path / "child.pid"
        fake_ckan(f"sleep 60 & echo $! > {pid_file}; echo started; wait")
        worker.timeout = 1
        run_id = _trigger(sysadmin, job_factory())

        worker.process_queue()

        run = _run(run_id)
        assert run.status == "failed"
        assert run.exit_code is None
        assert run.stdout == "started\n"
        assert "Killed after running for 1 seconds" in run.stderr

        child = int(pid_file.read_text())
        for _ in range(20):
            if not _alive(child):
                break
            time.sleep(0.1)
        assert not _alive(child)

    def test_terminating_worker_kills_the_command(self, worker, job_factory, sysadmin, fake_ckan):
        fake_ckan("sleep 60")
        run_id = _trigger(sysadmin, job_factory())
        worker.terminating = True

        started = time.monotonic()
        worker.execute(claim_run(worker.name))

        assert time.monotonic() - started < 10
        run = _run(run_id)
        assert run.status == "failed"
        assert "Terminated because the worker was stopped" in run.stderr

    def test_output_that_is_not_utf8(self, worker, job_factory, sysadmin, fake_ckan):
        fake_ckan(r"printf 'caf\351 \000ok'; printf 'bad\377' >&2")
        run_id = _trigger(sysadmin, job_factory())

        worker.process_queue()

        run = _run(run_id)
        assert run.status == "succeeded"
        assert run.stdout == "caf\ufffd ok"
        assert run.stderr == "bad\ufffd"

    def test_long_output_keeps_the_end(self, worker, job_factory, sysadmin, fake_ckan, monkeypatch):
        monkeypatch.setattr(worker_module, "MAX_OUTPUT_SIZE", 10)
        fake_ckan("echo 0123456789abcdefghij")
        run_id = _trigger(sysadmin, job_factory())

        worker.process_queue()

        assert _run(run_id).stdout == "…bcdefghij\n"

    def test_unexpected_error_fails_the_run(self, worker, job_factory, sysadmin, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(worker_module.subprocess, "Popen", explode)
        run_id = _trigger(sysadmin, job_factory())

        assert worker.process_queue() == 1

        run = _run(run_id)
        assert run.status == "failed"
        assert "The worker failed: boom" in run.stderr

    def test_heartbeat(self, worker, job_factory, sysadmin, fake_ckan, monkeypatch):
        monkeypatch.setattr(worker_module, "HEARTBEAT_INTERVAL", 0)
        fake_ckan("sleep 2")
        run_id = _trigger(sysadmin, job_factory())
        claim_run(worker.name)
        model.Session.execute(sa.update(CronRun).values(heartbeat=datetime.now(timezone.utc) - timedelta(hours=1)))
        model.Session.commit()

        worker.execute(run_id)

        assert _run(run_id).heartbeat > datetime.now(timezone.utc) - timedelta(minutes=1)

    def test_worker_is_listed_while_it_runs(self, worker, job_factory, sysadmin, fake_ckan):
        fake_ckan("sleep 1")
        run_id = _trigger(sysadmin, job_factory())
        seen = []
        execute = worker._execute

        def spy(run_id):
            seen.extend(call_action("cron_worker_list", {"ignore_auth": True}))
            execute(run_id)

        worker._execute = spy
        worker.run(once=True)

        assert [(w["name"], w["run_id"]) for w in seen] == [(worker.name, run_id)]
        assert call_action("cron_worker_list", {"ignore_auth": True}) == []

    def test_idle_worker_sends_heartbeats(self, worker, monkeypatch):
        monkeypatch.setattr(worker_module, "HEARTBEAT_INTERVAL", 0)
        worker.poll_interval = 1
        model.Session.add(CronWorker(name=worker.name, last_seen=datetime.now(timezone.utc) - timedelta(minutes=5)))
        model.Session.commit()

        worker._wait()

        assert [w["name"] for w in call_action("cron_worker_list", {"ignore_auth": True})] == [worker.name]

    def test_lost_workers_are_forgotten(self, worker):
        model.Session.add(CronWorker(name="gone:1", last_seen=datetime.now(timezone.utc) - timedelta(minutes=5)))
        model.Session.commit()

        worker.process_queue()

        assert model.Session.get(CronWorker, "gone:1") is None

    def test_runs_of_lost_workers_are_failed(self, worker, job_factory, sysadmin):
        job = job_factory()
        lost = CronRun(
            job_id=job["id"],
            status="running",
            worker="gone:1",
            started=datetime.now(timezone.utc) - timedelta(hours=1),
            heartbeat=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        alive = CronRun(job_id=job["id"], status="running", worker="alive:1", heartbeat=datetime.now(timezone.utc))
        model.Session.add_all([lost, alive])
        model.Session.commit()
        lost_id, alive_id = lost.id, alive.id

        worker.process_queue()

        assert _run(lost_id).status == "failed"
        assert "gone:1 stopped responding" in _run(lost_id).stderr
        assert _run(alive_id).status == "running"

    def test_command_that_became_invalid(self, worker, job_factory, sysadmin):
        run_id = _trigger(sysadmin, job_factory())
        model.Session.execute(sa.text("UPDATE cron_job SET command = 'no-such-command'"))
        model.Session.commit()

        worker.process_queue()

        run = _run(run_id)
        assert run.status == "failed"
        assert "Unknown ckan command" in run.stderr

    def test_missing_executable(self, worker, job_factory, sysadmin, monkeypatch):
        monkeypatch.setitem(tk.config, "ckanext.cron.worker.executable", "/no/such/ckan")
        run_id = _trigger(sysadmin, job_factory())

        worker.process_queue()

        assert "Cannot start /no/such/ckan" in _run(run_id).stderr

    def test_claim_only_queued(self, job_factory, sysadmin):
        run_id = _trigger(sysadmin, job_factory())

        assert claim_run("test") == run_id
        assert claim_run("test") is None
        assert _run(run_id).status == "running"

    def test_wakes_up_on_notify(self, worker, job_factory, sysadmin):
        worker._get_listener()
        _trigger(sysadmin, job_factory())

        started = time.monotonic()
        worker._wait()

        assert time.monotonic() - started < worker.poll_interval

    def test_pg_cron_enqueues_runs(self, worker, job_factory):
        """End to end: pg_cron enqueues a run on schedule and the worker executes it."""
        job = job_factory(command="cron call-action status_show", schedule="1 seconds")

        for _ in range(30):
            model.Session.rollback()
            run_id = model.Session.scalar(sa.select(CronRun.id).where(CronRun.job_id == job["id"]).limit(1))
            if run_id:
                break
            time.sleep(0.5)
        else:
            pytest.fail("pg_cron did not enqueue a run within 15 seconds")

        call_action("cron_job_update", {"ignore_auth": True}, id=job["id"], active=False)
        model.Session.execute(sa.delete(CronRun).where(CronRun.id != run_id))
        model.Session.commit()

        worker.process_queue()

        run = _run(run_id)
        assert run.trigger == "schedule"
        assert run.status == "succeeded", run.stderr
