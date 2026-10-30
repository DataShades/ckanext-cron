[![Tests](https://github.com/DataShades/ckanext-cron/workflows/Tests/badge.svg?branch=main)](https://github.com/DataShades/ckanext-cron/actions)

# ckanext-cron

Schedule `ckan` commands from the CKAN UI, the way you would in a crontab, with
[pg_cron](https://github.com/citusdata/pg_cron) as the scheduler.

```
Name:      Harvest sources
Command:   ckan harvester run
Schedule:  */30 * * * *
```

## How it works

1. A job is a `ckan` command plus a schedule. The command is stored in the CKAN
   database. The schedule lives in a pg_cron job that belongs to it.
2. When the schedule fires, pg_cron inserts a row into the `cron_run` table of the
   CKAN database and sends a `NOTIFY`. Runs don't overlap: while the previous run
   of the job is still queued or running, the new one is skipped, unless the job
   has "Allow overlapping runs" on. The same goes for "Run now".
3. `ckan cron worker` picks the run up and runs `ckan -c <its config> <command>`
   in a subprocess. The exit code, stdout and stderr go into the run history.

Sysadmins manage everything at `/ckan-admin/cron/` (there's a clock icon in the
header):

* **Jobs**: each command with its schedule described in words, active flag and
  latest run. Row actions: run now, edit, run history, enable/disable, delete.
  Bulk actions: enable, disable, delete. The edit form shows who created and
  last changed the job.
* **Run history** (`/ckan-admin/cron/runs`): status, exit code, timing, output and
  errors of every run, and who started the manual ones. Filter, export, or purge runs older than
  `ckanext.cron.history_retention_days`. Purging also cleans pg_cron's own log
  of the runs (`cron.job_run_details`), which pg_cron never deletes. If you don't
  need that log at all, `cron.log_run = off` in `postgresql.conf` turns it off.

The pages list the running workers, and warn when pg_cron is unreachable or
lacks permissions, when no worker is running, and when runs wait in the queue
because every worker is busy.

### Commands

Any `ckan` command that the worker's CKAN instance knows works, including those
added by extensions, e.g. `harvester run`, `search-index rebuild` or
`jobs clear`. The form suggests them. Commands are checked when a job is saved:
an unknown command, a group without a subcommand, a config file option, and
commands that never finish (`run`, `shell`, `cron worker`, `jobs worker`,
`asset watch`) are rejected.

A sysadmin who can schedule any command could do things on the server that
CKAN doesn't otherwise let a sysadmin do, such as rewrite the config file with
`config-tool`. So these commands are forbidden by default:

    config-tool, datastore dump, db, generate, sass, translation

Change the list with `ckanext.cron.forbidden_commands`, or allow only some
commands with `ckanext.cron.allowed_commands`. Each entry covers the commands
that start with it, so `db` covers `db upgrade`. The worker checks the command
again before every run, so tightening either setting also stops existing jobs.

To call a CKAN action on a schedule, use the `cron call-action` command:

    cron call-action harvest_source_reindex id=my-source

It calls the action as the site user with the `key=value` pairs as its data dict
(values that are valid JSON are decoded) and prints the result as JSON.

## Requirements

* CKAN 2.11+ (developed against 2.12)
* PostgreSQL with pg_cron 1.5+, on the same server as the CKAN database
* The `tables` plugin from [ckanext-tables](https://github.com/DataShades/ckanext-tables)

## Installation

1. Set up pg_cron on the PostgreSQL server (`postgresql.conf`, then restart):

       shared_preload_libraries = 'pg_cron'
       cron.database_name = 'postgres'   # the database pg_cron lives in
       # cron.timezone = 'UTC'            # schedules are in GMT by default

   then, as a superuser, in that database:

       CREATE EXTENSION pg_cron;

   When a schedule fires, pg_cron opens a new connection to the CKAN database as
   the owner of the job, through libpq to `cron.host` (`localhost` by default).
   `pg_hba.conf` has to let that role in without a password prompt, with a
   `trust` line for localhost or a `.pgpass` file for the `postgres` system user.
   Alternatively, `cron.use_background_workers = on` runs jobs in background
   workers that need no connection (raise `max_worker_processes` to match).
   If this is wrong, the jobs page shows the error of the failed pg_cron run
   instead of the latest run.

2. Install the extension and enable the plugins, `tables` included:

       pip install -e .

       ckan.plugins = ... tables cron

3. Create the tables:

       ckan db upgrade -p cron

4. If pg_cron isn't installed in the CKAN database itself, point the extension at
   the database where it is (see the config settings below).

5. Run a worker next to CKAN, under the same supervisor as your other CKAN
   processes:

       ckan -c /etc/ckan/default/ckan.ini cron worker

   Several workers can share the queue; each run is executed once.

   The first SIGTERM or SIGINT lets the current command finish before the worker
   exits, the second one terminates the command. On Linux a command never outlives
   its worker: if the worker dies, the kernel kills the command. Elsewhere the
   command keeps running after its run is marked as failed.

   Workers record a heartbeat every 10 seconds, also while a command runs. A run
   without one for a minute is marked as failed by the next worker that checks the
   queue, and a worker without one for a minute disappears from the jobs page.

### Permissions

pg_cron jobs are owned by the role in `ckanext.cron.database_url`. When pg_cron
fires, it connects to the CKAN database as that role to enqueue a run, so the
role needs to be able to read `cron_job` and `cron_run` and insert into
`cron_run`. That's already the case when it is the CKAN database role.

By default pg_cron lets an ordinary role call only `cron.schedule` and
`cron.unschedule`. This extension also needs `cron.schedule_in_database` and
`cron.alter_job`. A role that isn't a superuser needs these grants, run by a
superuser in the database where pg_cron is installed:

```sql
GRANT USAGE ON SCHEMA cron TO ckan_default;
GRANT EXECUTE ON FUNCTION
    cron.schedule_in_database(text,text,text,text,text,boolean),
    cron.alter_job(bigint,text,text,text,text,boolean),
    cron.unschedule(bigint)
TO ckan_default;
```

If any of these is missing, the jobs page shows the exact statement to run.

### Checking pg_cron

    ckan cron check [--fix]

reports jobs without a pg_cron job, pg_cron jobs left behind by deleted jobs
(after a failed save or a database restore, for example) and pg_cron jobs whose
command is out of date. `--fix` unschedules the leftovers and refreshes the
outdated commands. It exits with status 1 while a problem remains, so it also
works as a monitoring check. `ckan db downgrade -p cron` unschedules the
pg_cron jobs before dropping the tables.

Jobs run commands on the server with the worker's permissions, so the pages and
the API are sysadmin-only.

## Config settings

```ini
# Connection URL of the database where pg_cron is installed (cron.database_name).
# It must be on the same PostgreSQL server as the CKAN database.
# (optional, default: sqlalchemy.url)
ckanext.cron.database_url = postgresql://ckan_default:pass@localhost/postgres

# Seconds between queue checks by the worker. The worker also wakes up on every
# NOTIFY from pg_cron, so this only matters if a notification is missed.
# (optional, default: 30)
ckanext.cron.worker.poll_interval = 30

# Seconds a command may run before the worker kills it.
# (optional, default: 3600)
ckanext.cron.worker.timeout = 3600

# The ckan executable that runs commands.
# (optional, default: the ckan next to the worker's Python, then the one on PATH)
ckanext.cron.worker.executable = /usr/lib/ckan/default/bin/ckan

# "Purge old runs" on the run history page deletes runs older than this.
# (optional, default: 30)
ckanext.cron.history_retention_days = 30

# Commands that jobs may not run, separated by commas or newlines. `db` also
# covers `db upgrade` and the other `db` subcommands. Empty allows them all.
# (optional, default: config-tool, datastore dump, db, generate, sass, translation)
ckanext.cron.forbidden_commands = config-tool, db, user token

# If set, jobs may only run these commands (forbidden_commands still applies).
# (optional, default: any command)
ckanext.cron.allowed_commands = harvester run, search-index rebuild, cron call-action
```

## API

All actions are sysadmin-only.

| Action             | Parameters                                                      |
| ------------------ | --------------------------------------------------------------- |
| `cron_info`        | pg_cron availability, version, timezone, connected role         |
| `cron_job_list`    |                                                                 |
| `cron_worker_list` | the workers that sent a heartbeat in the last minute, with the run each one executes |
| `cron_job_show`    | `id`                                                            |
| `cron_job_create`  | `name`, `command`, `schedule`, `active`?, `allow_overlap`?      |
| `cron_job_update`  | `id`, `name`?, `command`?, `schedule`?, `active`?, `allow_overlap`? |
| `cron_job_delete`  | `id` (also deletes the job's run history)                       |
| `cron_job_trigger` | `id`: queue a run now, regardless of the schedule (fails while a run is unfinished, unless the job allows overlap) |
| `cron_run_list`    | `job_id`?, `status`?, `limit`? (default 100, max 1000), `offset`? |
| `cron_run_show`    | `id`                                                            |
| `cron_run_purge`   | `job_id`?, `days`? (default `ckanext.cron.history_retention_days`); also purges `cron.job_run_details` |

Schedules accept cron syntax (`*/30 * * * *`), intervals from 1 to 59 seconds
(`30 seconds`), macros such as `@hourly` or `@daily`, and `$` as the day of month
for the last day of the month.

## Tests

Most tests need a PostgreSQL server with pg_cron, reachable through
`ckanext.cron.database_url`, and skip themselves when it isn't (on CI, with
`CI` set, they fail instead). Tests marked `without_pg_cron` always run. CI uses
the `supabase/postgres` image, which preloads pg_cron, and prepares it with
`.github/scripts/setup_pg_cron.py`. Locally, set `ckanext.cron.database_url` in
`test.ini` to a pg_cron-enabled server, then run:

    pytest --ckan-ini=test.ini

## License

[AGPL](https://www.gnu.org/licenses/agpl-3.0.en.html)
