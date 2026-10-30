from __future__ import annotations

import pytest

from ckanext.cron import helpers

pytestmark = pytest.mark.without_pg_cron


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("@hourly", "Every hour"),
        ("@REBOOT", "When pg_cron starts"),
        ("30 seconds", "Every 30 seconds"),
        ("*/30 * * * *", "Every 30 minutes"),
        ("0 3 $ * *", "last day of the month"),
    ],
)
def test_explain_schedule(schedule, expected):
    assert expected in helpers.cron_explain_schedule(schedule)


@pytest.mark.parametrize("schedule", ["not a schedule", "", None])
def test_explain_unknown_schedule(schedule):
    assert helpers.cron_explain_schedule(schedule) == ""


def test_command_suggestions():
    assert "search-index rebuild" in helpers.cron_command_suggestions()


def test_runs_url_filters_by_job(with_request_context):
    url = helpers.cron_runs_url("abc")

    assert "field-cron_runs=job_id" in url
    assert "value-cron_runs=abc" in url
