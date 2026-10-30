"""Executes queued runs: each one runs the job's `ckan` command in a subprocess."""

from __future__ import annotations

import ctypes
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import IO, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from ckan import model

from ckanext.cron import job_commands
from ckanext.cron.model import LOST_AFTER, NOTIFY_CHANNEL, CronJob, CronRun, CronWorker

log = logging.getLogger(__name__)

# only the last part of longer stdout or stderr is kept, in bytes
MAX_OUTPUT_SIZE = 100_000

# seconds between heartbeats of the worker and of the command it runs
HEARTBEAT_INTERVAL = 10

# seconds a command gets to exit after SIGTERM before it is killed
KILL_GRACE = 5

PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None


class Worker:
    def __init__(self, config_file: str, poll_interval: int, timeout: int):
        self.config_file = config_file
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        # set by the first SIGINT or SIGTERM: finish the current command, then exit
        self.stopping = False
        # set by the second one: terminate the current command too
        self.terminating = False
        self._listener: Any = None

    def run(self, once: bool = False) -> None:
        """Process queued runs until stopped, or until the queue is empty if `once` is set.

        The first SIGINT or SIGTERM lets the current command finish before the
        worker exits, the second one terminates the command.
        """
        _register(self.name)

        if once:
            try:
                self.process_queue()
            finally:
                _unregister(self.name)
            return

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        log.info("Cron worker %s is listening on %s", self.name, NOTIFY_CHANNEL)

        try:
            while not self.stopping:
                self.process_queue()
                self._wait()
        finally:
            self._close_listener()
            _unregister(self.name)

        log.info("Cron worker %s has stopped", self.name)

    def _stop(self, signum: int, frame: Any) -> None:
        if self.stopping:
            log.info("Cron worker %s is terminating the current command", self.name)
            self.terminating = True
        else:
            log.info("Cron worker %s is stopping after the current command, signal again to terminate it", self.name)
            self.stopping = True

    def process_queue(self) -> int:
        """Execute queued runs one by one until none is left. Return how many ran."""
        if lost := fail_lost_runs():
            log.warning("Marked %s cron runs of workers that stopped responding as failed", lost)

        forget_lost_workers()

        count = 0

        while not self.stopping and (run_id := claim_run(self.name)) is not None:
            self.execute(run_id)
            count += 1

        return count

    def execute(self, run_id: int) -> None:
        """Run the command of the run's job and record its outcome, whatever goes wrong."""
        _heartbeat(self.name, run_id)

        try:
            self._execute(run_id)
        except Exception as e:
            log.exception("Cron run %s: the worker failed", run_id)
            model.Session.remove()

            try:
                _finish(run_id, None, "", f"The worker failed: {e}")
            except Exception:
                # the run will be failed as lost once its heartbeat is old enough
                log.exception("Cron run %s: cannot record the failure", run_id)
                model.Session.remove()

        _heartbeat(self.name, None)

    def _execute(self, run_id: int) -> None:
        run = model.Session.get(CronRun, run_id)
        job = model.Session.get(CronJob, run.job_id) if run else None

        if not run or not job:
            log.warning("Cron run %s has no job, skipping it", run_id)
            _finish(run_id, None, "", "The job of this run does not exist")
            return

        command = job.command
        model.Session.remove()

        try:
            argv = job_commands.argv(command, self.config_file)
        except job_commands.InvalidCommand as e:
            log.warning("Cron run %s: invalid command %r: %s", run_id, command, e)
            _finish(run_id, None, "", str(e))
            return

        log.info("Cron run %s: running ckan %s", run_id, command)

        # output goes to files, so that a chatty command doesn't fill the worker's memory
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                # a session of its own keeps Ctrl+C and SIGTERM sent to the worker away from the command,
                # and lets the worker kill everything the command starts
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    preexec_fn=_die_with_worker,
                )
            except OSError as e:
                log.exception("Cron run %s: cannot start %s", run_id, argv[0])
                _finish(run_id, None, "", f"Cannot start {argv[0]}: {e}")
                return

            exit_code, note = self._supervise(proc, run_id)
            _finish(run_id, exit_code, _read_tail(stdout), _read_tail(stderr) + note)

    def _supervise(self, proc: subprocess.Popen[bytes], run_id: int) -> tuple[int | None, str]:
        """Wait for the command while sending heartbeats. Return its exit code, or None and the reason it was killed."""
        deadline = time.monotonic() + self.timeout
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL

        while True:
            try:
                exit_code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            else:
                log.info("Cron run %s: exited with %s", run_id, exit_code)
                return exit_code, ""

            if self.terminating:
                log.warning("Cron run %s: terminated because the worker is stopping", run_id)
                _kill(proc)
                return None, "\nTerminated because the worker was stopped"

            if time.monotonic() >= deadline:
                log.warning("Cron run %s: killed after %s seconds", run_id, self.timeout)
                _kill(proc)
                return None, f"\nKilled after running for {self.timeout} seconds"

            if time.monotonic() >= next_heartbeat:
                _heartbeat(self.name, run_id)
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL

    def _wait(self) -> None:
        """Sleep until pg_cron sends a NOTIFY, the poll interval passes, or the worker is stopped."""
        deadline = time.monotonic() + self.poll_interval
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL

        while not self.stopping and time.monotonic() < deadline:
            if time.monotonic() >= next_heartbeat:
                _heartbeat(self.name, None)
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL

            try:
                conn = self._get_listener()
                if select.select([conn], [], [], 1)[0]:
                    conn.poll()
                    if conn.notifies:
                        conn.notifies.clear()
                        return
            except Exception:
                log.exception("Lost the LISTEN connection, reconnecting")
                self._close_listener()
                time.sleep(1)

    def _get_listener(self) -> Any:
        if self._listener is None:
            raw = model.meta.engine.raw_connection()
            conn = raw.driver_connection
            conn.autocommit = True
            conn.cursor().execute(f"LISTEN {NOTIFY_CHANNEL}")
            self._listener = raw

        return self._listener.driver_connection

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.invalidate()
            finally:
                self._listener = None


def claim_run(worker_name: str) -> int | None:
    """Mark the oldest queued run as running and return its id.

    SKIP LOCKED lets several workers share the queue without taking the same run.
    """
    oldest = (
        sa.select(CronRun.id)
        .where(CronRun.status == CronRun.Status.queued)
        .order_by(CronRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        sa.update(CronRun)
        .where(CronRun.id == oldest)
        .values(status=CronRun.Status.running, started=sa.func.now(), heartbeat=sa.func.now(), worker=worker_name)
        .returning(CronRun.id)
    )

    run_id = model.Session.execute(stmt).scalar()
    model.Session.commit()

    return run_id


def fail_lost_runs() -> int:
    """Mark running runs whose worker stopped sending heartbeats as failed. Return how many."""
    last_seen = sa.func.coalesce(CronRun.heartbeat, CronRun.started)
    stmt = (
        sa.update(CronRun)
        .where(CronRun.status == CronRun.Status.running, last_seen < sa.func.now() - LOST_AFTER)
        .values(
            status=CronRun.Status.failed,
            finished=sa.func.now(),
            stderr=sa.func.concat("The worker ", CronRun.worker, " stopped responding while running the command"),
        )
    )

    count = model.Session.execute(stmt).rowcount
    model.Session.commit()
    model.Session.remove()

    return count


def forget_lost_workers() -> None:
    """Remove the workers that stopped sending heartbeats without unregistering."""
    model.Session.execute(sa.delete(CronWorker).where(CronWorker.last_seen < sa.func.now() - LOST_AFTER))
    model.Session.commit()
    model.Session.remove()


def _register(worker_name: str) -> None:
    model.Session.execute(_upsert_worker(worker_name, None, {"started": sa.func.now()}))
    model.Session.commit()
    model.Session.remove()


def _upsert_worker(worker_name: str, run_id: int | None, extra: dict[str, Any] | None = None) -> Any:
    """Record that the worker is alive, adding it again if it was forgotten while unreachable."""
    stmt = insert(CronWorker).values(name=worker_name, run_id=run_id)
    return stmt.on_conflict_do_update(
        index_elements=[CronWorker.name], set_={"last_seen": sa.func.now(), "run_id": run_id, **(extra or {})}
    )


def _unregister(worker_name: str) -> None:
    try:
        model.Session.execute(sa.delete(CronWorker).where(CronWorker.name == worker_name))
        model.Session.commit()
    except sa.exc.SQLAlchemyError:
        log.warning("Cron worker %s: cannot unregister", worker_name, exc_info=True)
    finally:
        model.Session.remove()


def _heartbeat(worker_name: str, run_id: int | None) -> None:
    """Show that the worker is alive, and that it is still running `run_id` if given."""
    try:
        model.Session.execute(_upsert_worker(worker_name, run_id))

        if run_id is not None:
            model.Session.execute(sa.update(CronRun).where(CronRun.id == run_id).values(heartbeat=sa.func.now()))

        model.Session.commit()
    except sa.exc.SQLAlchemyError:
        log.warning("Cron worker %s: cannot record a heartbeat", worker_name, exc_info=True)
    finally:
        model.Session.remove()


def _finish(run_id: int, exit_code: int | None, stdout: str, stderr: str) -> None:
    status = CronRun.Status.succeeded if exit_code == 0 else CronRun.Status.failed
    stmt = (
        sa.update(CronRun)
        .where(CronRun.id == run_id)
        .values(
            status=status,
            finished=sa.func.now(),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
    )
    model.Session.execute(stmt)
    model.Session.commit()
    model.Session.remove()


def _die_with_worker() -> None:
    """Runs in the command's process before exec: make Linux kill it if the worker dies."""
    if _libc is not None:
        _libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Stop the command and everything it started: SIGTERM first, SIGKILL if it doesn't exit in time."""
    _signal_group(proc, signal.SIGTERM)

    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass

    # whatever is left of the process group
    _signal_group(proc, signal.SIGKILL)
    proc.wait()


def _signal_group(proc: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(proc.pid, signum)
    except ProcessLookupError:
        pass


def _read_tail(output: IO[bytes]) -> str:
    """Return the end of a command's output as text that PostgreSQL accepts.

    Bytes that aren't UTF-8 are replaced, and NUL characters are dropped.
    """
    size = output.seek(0, os.SEEK_END)
    output.seek(max(0, size - MAX_OUTPUT_SIZE))
    text = output.read().decode("utf-8", errors="replace").replace("\x00", "")

    return "…" + text if size > MAX_OUTPUT_SIZE else text
