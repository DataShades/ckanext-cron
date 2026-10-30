from __future__ import annotations

from ckan import types
from ckan.logic.schema import validator_args

from ckanext.cron import config
from ckanext.cron.model import CronRun


@validator_args
def cron_job_create(
    not_empty: types.Validator,
    default: types.ValidatorFactory,
    unicode_safe: types.Validator,
    boolean_validator: types.Validator,
    cron_strip: types.Validator,
    cron_job_name_available: types.Validator,
    cron_command: types.Validator,
) -> types.Schema:
    return {
        "name": [not_empty, unicode_safe, cron_strip, cron_job_name_available],
        "command": [not_empty, unicode_safe, cron_command],
        "schedule": [not_empty, unicode_safe, cron_strip],
        "active": [default(True), boolean_validator],
        "allow_overlap": [default(False), boolean_validator],
    }


@validator_args
def cron_job_update(
    not_empty: types.Validator,
    ignore_missing: types.Validator,
    unicode_safe: types.Validator,
    boolean_validator: types.Validator,
    cron_strip: types.Validator,
    cron_job_exists: types.Validator,
    cron_job_name_available: types.Validator,
    cron_command: types.Validator,
) -> types.Schema:
    return {
        "id": [not_empty, unicode_safe, cron_job_exists],
        "name": [ignore_missing, not_empty, unicode_safe, cron_strip, cron_job_name_available],
        "command": [ignore_missing, not_empty, unicode_safe, cron_command],
        "schedule": [ignore_missing, not_empty, unicode_safe, cron_strip],
        "active": [ignore_missing, boolean_validator],
        "allow_overlap": [ignore_missing, boolean_validator],
    }


@validator_args
def cron_job_id(not_empty: types.Validator, unicode_safe: types.Validator, cron_job_exists: types.Validator) -> types.Schema:
    return {"id": [not_empty, unicode_safe, cron_job_exists]}


@validator_args
def cron_run_show(not_empty: types.Validator, int_validator: types.Validator) -> types.Schema:
    return {"id": [not_empty, int_validator]}


@validator_args
def cron_run_list(
    ignore_missing: types.Validator,
    default: types.ValidatorFactory,
    unicode_safe: types.Validator,
    natural_number_validator: types.Validator,
    one_of: types.ValidatorFactory,
) -> types.Schema:
    statuses = [CronRun.Status.queued, CronRun.Status.running, CronRun.Status.succeeded, CronRun.Status.failed]
    return {
        "job_id": [ignore_missing, unicode_safe],
        "status": [ignore_missing, unicode_safe, one_of(statuses)],
        "limit": [default(100), natural_number_validator],
        "offset": [default(0), natural_number_validator],
    }


@validator_args
def cron_run_purge(
    ignore_missing: types.Validator,
    default: types.ValidatorFactory,
    unicode_safe: types.Validator,
    natural_number_validator: types.Validator,
    cron_job_exists: types.Validator,
) -> types.Schema:
    return {
        "job_id": [ignore_missing, unicode_safe, cron_job_exists],
        "days": [default(config.history_retention_days()), natural_number_validator],
    }
