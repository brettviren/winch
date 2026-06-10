#!/usr/bin/env pytest
'''
Tests for the "run" taxon: config parsing, run resolution (including
{layer.NAME.VAR} cross-references and free-variable formatting), command-line
assembly, and the "winch run" CLI command (podman execution stubbed).
'''

import tomllib

import pytest
from click.testing import CliRunner

import winch.cli as cli_mod
from winch.cli import cli
from winch.config import parse, parse_runs, ConfigError
from winch.runs import (resolve_run, build_run_command, split_passthrough)


CFG = """
[layer.debian]
release = "bookworm"
provides = ["os:debian"]
containerfile = "FROM debian:{release}\\n"

[layer.devel_user]
uid = 1000
gid = 1000
provides = ["devuser"]
body = "RUN useradd -u {uid} -g {gid} devel\\n"

[recipe.dev]
stack = ["debian", "devel_user"]

[run.dev]
description = "developer shell"
image = "recipe.dev"
uid = "{layer.devel_user.uid}"
gid = "{layer.devel_user.gid}"
podman_args = ["--userns=keep-id:uid={uid},gid={gid}"]
command = "/bin/bash"
volumes = ["/home/me/work:/devel:z"]
"""


def _parsed(text=CFG):
    cfg = tomllib.loads(text)
    _, layers, recipes = parse(cfg)
    runs = parse_runs(cfg)
    return layers, recipes, runs


# --- parsing ----------------------------------------------------------------

def test_parse_runs_basic():
    _, _, runs = _parsed()
    assert "dev" in runs
    run = runs["dev"]
    assert run.image == "recipe.dev"
    assert run.command == "/bin/bash"
    assert run.volumes == ["/home/me/work:/devel:z"]
    assert run.podman_args == ["--userns=keep-id:uid={uid},gid={gid}"]
    # uid/gid are free run variables, not special keys.
    assert run.vars == {"uid": "{layer.devel_user.uid}",
                        "gid": "{layer.devel_user.gid}"}


def test_run_requires_image():
    with pytest.raises(ConfigError):
        parse_runs(tomllib.loads('[run.x]\ndescription = "no image"\n'))


def test_run_namespace_does_not_trip_paradigm_detector():
    # A runs-only config must parse as "new", not be flagged an old/new mix.
    cfg = tomllib.loads('[run.x]\nimage = "alpine"\n')
    paradigm, layers, recipes = parse(cfg)
    assert paradigm == "new"


# --- resolution -------------------------------------------------------------

def test_resolve_layer_refs_and_free_vars():
    layers, recipes, runs = _parsed()
    r = resolve_run(runs["dev"], layers, recipes)
    # {layer.devel_user.uid} -> 1000, then {uid}/{gid} -> the podman arg.
    assert r.podman_args == ["--userns=keep-id:uid=1000,gid=1000"]
    assert r.volumes == ["/home/me/work:/devel:z"]
    assert r.command == "/bin/bash"
    # image resolves to the recipe's leaf (devel_user) digest image.
    assert r.image.startswith("localhost/winch/devel_user:")


def test_resolve_direct_image_no_layer_refs():
    text = '[run.x]\nimage = "alpine:3"\ncommand = "sh"\n'
    runs = parse_runs(tomllib.loads(text))
    r = resolve_run(runs["x"], {}, {})
    assert r.image == "alpine:3"
    assert r.command == "sh"


def test_resolve_layer_ref_on_direct_image_errors():
    text = '[run.x]\nimage = "alpine"\nfoo = "{layer.debian.release}"\npodman_args = ["{foo}"]\n'
    runs = parse_runs(tomllib.loads(text))
    with pytest.raises(ConfigError):
        resolve_run(runs["x"], {}, {})


def test_resolve_unknown_layer_var_errors():
    layers, recipes, _ = _parsed()
    text = '[run.x]\nimage = "recipe.dev"\nbad = "{layer.devel_user.nope}"\npodman_args = ["{bad}"]\n'
    runs = parse_runs(tomllib.loads(text))
    with pytest.raises(ConfigError):
        resolve_run(runs["x"], layers, recipes)


def test_resolve_unresolved_free_var_errors():
    layers, recipes, _ = _parsed()
    text = '[run.x]\nimage = "recipe.dev"\npodman_args = ["{typo}"]\n'
    runs = parse_runs(tomllib.loads(text))
    with pytest.raises(ConfigError):
        resolve_run(runs["x"], layers, recipes)


# --- command assembly -------------------------------------------------------

def test_split_passthrough():
    assert split_passthrough(["--rm", "-it"]) == (["--rm", "-it"], None)
    assert split_passthrough(["--rm", "--", "ls", "-l"]) == (["--rm"], ["ls", "-l"])
    assert split_passthrough(["--", "ls"]) == ([], ["ls"])
    assert split_passthrough([]) == ([], None)


def test_build_run_command_order():
    layers, recipes, runs = _parsed()
    r = resolve_run(runs["dev"], layers, recipes)
    argv = build_run_command(r, ["--rm", "-it"])
    # podman_args, then extra opts, then -v pairs, then image, then command.
    assert argv[0] == "--userns=keep-id:uid=1000,gid=1000"
    assert argv[1:3] == ["--rm", "-it"]
    assert "-v" in argv and "/home/me/work:/devel:z" in argv
    assert argv[-1] == "/bin/bash"
    assert argv[argv.index("-v") + 2] == r.image  # image right after the -v pair


def test_build_run_command_override():
    layers, recipes, runs = _parsed()
    r = resolve_run(runs["dev"], layers, recipes)
    argv = build_run_command(r, ["--rm"], ["bash", "-lc", "echo hi"])
    assert argv[-3:] == ["bash", "-lc", "echo hi"]
    assert "/bin/bash" not in argv  # configured command was overridden


# --- CLI --------------------------------------------------------------------

@pytest.fixture
def captured_run(monkeypatch):
    '''Capture the argv that "winch run" would exec instead of running podman.'''
    captured = {}
    monkeypatch.setattr(cli_mod, "run_image",
                        lambda argv: captured.setdefault("argv", list(argv)))
    return captured


def _invoke(*argv):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(CFG)
        return runner.invoke(cli, ["-c", "w.toml", "run", *argv])


def test_cli_run_execs_assembled_command(captured_run):
    res = _invoke("dev", "--rm", "-it")
    assert res.exit_code == 0, res.output
    argv = captured_run["argv"]
    assert argv[0] == "--userns=keep-id:uid=1000,gid=1000"
    assert "--rm" in argv and "-it" in argv
    assert argv[-1] == "/bin/bash"


def test_cli_run_command_after_dashdash(captured_run):
    res = _invoke("dev", "--rm", "-it", "--", "ls", "-la")
    assert res.exit_code == 0, res.output
    argv = captured_run["argv"]
    assert argv[-2:] == ["ls", "-la"]
    assert "/bin/bash" not in argv


def test_cli_run_dry_run_prints_and_does_not_exec(captured_run):
    res = _invoke("-n", "dev", "--rm")
    assert res.exit_code == 0, res.output
    assert res.output.startswith("podman run ")
    assert "--userns=keep-id:uid=1000,gid=1000" in res.output
    assert "argv" not in captured_run  # run_image was never called


def test_cli_run_unknown_name_errors(captured_run):
    res = _invoke("nope")
    assert res.exit_code != 0
    assert 'no such run "nope"' in res.output
