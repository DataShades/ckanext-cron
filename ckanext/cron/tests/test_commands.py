from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

import ckan.plugins.toolkit as tk
from ckan.cli.cli import ckan
from ckan.tests import factories

from ckanext.cron import job_commands
from ckanext.cron.config import CONF_ALLOWED_COMMANDS, CONF_FORBIDDEN_COMMANDS

pytestmark = pytest.mark.without_pg_cron


class TestParse:
    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("search-index rebuild", ["search-index", "rebuild"]),
            ("ckan search-index rebuild", ["search-index", "rebuild"]),
            ("jobs list --queue default", ["jobs", "list", "--queue", "default"]),
            ("cron call-action package_search 'q=a b'", ["cron", "call-action", "package_search", "q=a b"]),
        ],
    )
    def test_valid(self, command, args):
        assert job_commands.parse(command) == args

    @pytest.mark.parametrize(
        "command", ["", "ckan", "nope", "jobs", "jobs nope", "shell", "run", "cron worker", "jobs worker", "asset watch"]
    )
    def test_invalid(self, command):
        with pytest.raises(job_commands.InvalidCommand):
            job_commands.parse(command)

    @pytest.mark.parametrize("command", ["--help", "-h", "jobs --help", "jobs -h"])
    def test_group_options_do_not_exit_validation(self, command):
        with pytest.raises(job_commands.InvalidCommand):
            job_commands.parse(command)

    @pytest.mark.parametrize(
        "command", ["config-tool /etc/ckan.ini a=b", "db upgrade", "db clean --yes", "generate config /tmp/x.ini"]
    )
    def test_forbidden_by_default(self, command):
        with pytest.raises(job_commands.InvalidCommand, match="forbidden_commands"):
            job_commands.parse(command)

    @pytest.mark.ckan_config(CONF_FORBIDDEN_COMMANDS, "search-index clear,\n  user token")
    def test_configured_forbidden(self):
        assert job_commands.parse("db upgrade") == ["db", "upgrade"]
        assert job_commands.parse("user list") == ["user", "list"]

        for command in ["search-index clear", "user token list"]:
            with pytest.raises(job_commands.InvalidCommand, match="forbidden_commands"):
                job_commands.parse(command)

    @pytest.mark.ckan_config(CONF_ALLOWED_COMMANDS, "search-index, cron call-action")
    def test_configured_allowed(self):
        assert job_commands.parse("search-index rebuild") == ["search-index", "rebuild"]
        assert job_commands.parse("cron call-action status_show") == ["cron", "call-action", "status_show"]

        with pytest.raises(job_commands.InvalidCommand, match="allowed_commands"):
            job_commands.parse("jobs list")

    @pytest.mark.ckan_config(CONF_FORBIDDEN_COMMANDS, "")
    def test_nothing_forbidden(self):
        assert job_commands.parse("db upgrade") == ["db", "upgrade"]

    def test_argv(self):
        assert job_commands.argv("jobs list", "/etc/ckan.ini")[1:] == ["-c", "/etc/ckan.ini", "jobs", "list"]

    def test_configured_executable(self, monkeypatch):
        monkeypatch.setitem(tk.config, "ckanext.cron.worker.executable", "/opt/ckan/bin/ckan")

        assert job_commands.argv("jobs list", "/etc/ckan.ini")[0] == "/opt/ckan/bin/ckan"

    def test_click_resolution_preserves_aliases_and_policy(self, monkeypatch):
        class AliasedGroup(click.Group):
            def resolve_command(self, ctx, args):
                if args and args[0] == "start":
                    args = ["worker", *args[1:]]
                return super().resolve_command(ctx, args)

        group = AliasedGroup("cron")
        group.add_command(click.Command("worker"))
        monkeypatch.setattr(job_commands, "top_level_commands", lambda: {"cron": group})

        with pytest.raises(job_commands.InvalidCommand, match="can't run as a cron job"):
            job_commands.parse("cron start")

    def test_inspection_does_not_execute_callbacks(self, monkeypatch):
        def unexpected_callback(*args, **kwargs):
            pytest.fail("Command inspection executed a callback")

        group = click.Group("example", callback=unexpected_callback)
        group.add_command(click.Command("task", callback=unexpected_callback))
        monkeypatch.setattr(job_commands, "top_level_commands", lambda: {"example": group})

        assert job_commands.parse("example task") == ["example", "task"]
        assert job_commands.leaf_commands() == ["example task"]


def test_leaf_commands():
    names = job_commands.leaf_commands()

    assert "search-index rebuild" in names
    assert "cron call-action" in names
    assert "run" not in names
    assert "cron worker" not in names
    assert "db upgrade" not in names
    assert names == sorted(names)


@pytest.mark.ckan_config(CONF_ALLOWED_COMMANDS, "search-index rebuild")
def test_leaf_commands_are_limited_by_allowed_commands():
    assert job_commands.leaf_commands() == ["search-index rebuild"]


@pytest.mark.usefixtures("clean_db")
class TestCallAction:
    def _invoke(self, *args):
        return CliRunner().invoke(ckan, ["-c", tk.config["__file__"], "cron", "call-action", *args])

    def test_result_as_json(self):
        result = self._invoke("status_show")

        assert result.exit_code == 0, result.output
        assert '"site_title"' in result.output

    def test_params(self):
        factories.User(name="cron-param-user")

        result = self._invoke("user_show", "id=cron-param-user", "include_datasets=false")

        assert result.exit_code == 0, result.output
        assert '"name": "cron-param-user"' in result.output

    def test_runs_as_site_user(self):
        assert self._invoke("config_option_list").exit_code == 0

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (["no_such_action"], "does not exist"),
            (["user_show", "id=missing-user"], "NotFound"),
            (["package_create"], "Missing value"),
            (["status_show", "not-a-pair"], "is not a key=value pair"),
        ],
    )
    def test_errors(self, args, error):
        result = self._invoke(*args)

        assert result.exit_code != 0
        assert error in result.output
