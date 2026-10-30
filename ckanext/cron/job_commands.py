"""Discover CKAN commands, enforce job policy, and build worker arguments.

Click owns command resolution and execution. This module only inspects commands;
their callbacks and argument validation run in the worker subprocess.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from importlib.metadata import entry_points
from pathlib import Path

import click

import ckan.plugins as p
import ckan.plugins.toolkit as tk

from ckanext.cron import config

# commands that never finish on their own or need a terminal, whatever the config says
FORBIDDEN = {("run",), ("shell",), ("cron", "worker"), ("jobs", "worker"), ("asset", "watch")}


class InvalidCommand(ValueError):
    pass


def top_level_commands() -> dict[str, click.Command]:
    """Commands of the `ckan` CLI, including those added by extensions."""
    from ckan.cli.cli import ckan

    commands = dict(ckan.commands)

    for entry in entry_points(group="ckan.click_command"):
        commands[entry.name] = entry.load()

    for plugin in p.PluginImplementations(p.IClick):
        for cmd in plugin.get_commands():
            commands[cmd.name] = cmd

    return commands


def _matches(path: tuple[str, ...], prefixes: list[tuple[str, ...]]) -> bool:
    return any(path[: len(prefix)] == prefix for prefix in prefixes)


def is_allowed(path: tuple[str, ...]) -> bool:
    """Check a command path against ckanext.cron.allowed_commands and forbidden_commands.

    Both settings list command prefixes, so `db` covers `db upgrade` and every
    other `db` subcommand.
    """
    allowed = config.allowed_commands()

    if allowed and not _matches(path, allowed):
        return False

    return not _matches(path, config.forbidden_commands())


def _subcommands(group: click.Group) -> dict[str, click.Command]:
    ctx = click.Context(group)
    return {name: cmd for name in group.list_commands(ctx) if (cmd := group.get_command(ctx, name))}


def leaf_commands() -> list[str]:
    """Every runnable command path, such as `harvester run` or `search-index rebuild`."""
    result: list[str] = []

    def walk(path: tuple[str, ...], cmd: click.Command) -> None:
        if getattr(cmd, "hidden", False) or path in FORBIDDEN:
            return

        # a group is walked even if it isn't allowed itself, because allowed_commands may name its subcommands
        if not isinstance(cmd, click.Group) and not is_allowed(path):
            return

        if isinstance(cmd, click.Group):
            for name, sub in _subcommands(cmd).items():
                walk((*path, name), sub)
        else:
            result.append(" ".join(path))

    for name, cmd in top_level_commands().items():
        walk((name,), cmd)

    return sorted(result)


def parse(command: str) -> list[str]:
    """Split a command into arguments and check that it names a runnable `ckan` command.

    A leading `ckan` is dropped, and the config file option is not allowed because
    the worker always passes its own.
    """
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise InvalidCommand(str(e)) from None

    if args and args[0] == "ckan":
        args = args[1:]

    if not args:
        raise InvalidCommand(tk._("The command is empty"))

    if any(arg in ("-c", "--config") or arg.startswith("--config=") for arg in args):
        raise InvalidCommand(tk._("Don't pass a config file, the worker uses its own"))

    # A separate root avoids CKAN's config-loading callback and leaves the
    # process-wide CLI registry untouched. Preserve extension Group subclasses
    # so their custom command resolution (for example aliases) still works.
    cmd: click.Command = click.Group("ckan", commands=top_level_commands())
    remaining = args[:]
    path: list[str] = []
    ctx = click.Context(cmd)

    try:
        while isinstance(cmd, click.Group):
            if not remaining:
                raise click.UsageError("Missing command.", ctx)
            # resolve_command reparses option-like names, which can invoke
            # eager callbacks such as --help. Only resolve command names here.
            if remaining[0].startswith("-"):
                raise click.UsageError(tk._("Expected a command name, got an option."), ctx)
            name, subcommand, remaining = cmd.resolve_command(ctx, remaining)
            if subcommand is None:
                raise click.UsageError("Missing command.", ctx)
            path.append(name)
            cmd = subcommand
            ctx = click.Context(cmd, info_name=name, parent=ctx)
    except click.ClickException as e:
        raise InvalidCommand(e.format_message()) from None

    # Use resolved names so aliases cannot bypass the command policy.
    args = [*path, *remaining]

    if tuple(path) in FORBIDDEN:
        raise InvalidCommand(tk._("ckan {command} can't run as a cron job").format(command=" ".join(path)))

    if not is_allowed(tuple(path)):
        raise InvalidCommand(
            tk._("ckan {command} is not allowed by the ckanext.cron.allowed_commands or forbidden_commands setting").format(
                command=" ".join(path)
            )
        )

    return args


def ckan_executable() -> str:
    if configured := config.executable():
        return configured

    sibling = Path(sys.executable).with_name("ckan")

    if sibling.exists():
        return str(sibling)

    return shutil.which("ckan") or "ckan"


def argv(command: str, config_file: str) -> list[str]:
    """The full argument list that runs a job command with the given CKAN config."""
    return [ckan_executable(), "-c", config_file, *parse(command)]
