#!/usr/bin/env pytest
'''
Test recipe-driven list/dot/render commands (see doc/winch2-plan.md section
12.9).  No podman needed (inspection only).
'''

from click.testing import CliRunner

from winch.cli import cli


NEW_CFG = """
[layer.debian]
release = "trixie"
provides = ["os:debian"]
containerfile = "FROM debian:{release}\\n"
[layer.spack]
version = "v1.1.0"
provides = ["spack"]
requires = ["os:debian|os:alma"]
body = "RUN spack {version}\\n"
[recipe.phlex]
stack = ["debian", "spack"]
[recipe.other]
stack = ["debian"]
"""

OLD_CFG = """
[debian]
release = ['bookworm', 'trixie']
image = '{kind}:{release}'
"""


def invoke(cfg, *argv, write=None):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(cfg)
        res = runner.invoke(cli, ["-c", "w.toml", *argv])
        extra = open(write).read() if (write and res.exit_code == 0) else None
    return res, extra


# --- list -------------------------------------------------------------------

def test_list_bare_enumerates_layers_and_recipes():
    res, _ = invoke(NEW_CFG, "list")
    assert res.exit_code == 0, res.output
    assert "layer debian" in res.output
    assert "layer spack" in res.output
    assert "recipe phlex" in res.output
    assert "recipe other" in res.output


def test_list_named_recipe_chain():
    res, _ = invoke(NEW_CFG, "list", "phlex", "-t", "{kind} {image}")
    assert res.exit_code == 0, res.output
    lines = [l for l in res.output.splitlines() if l.strip()]
    assert lines[0].startswith("debian localhost/winch/debian:")
    assert lines[1].startswith("spack localhost/winch/spack:")


def test_list_anonymous_stack_with_set():
    res, _ = invoke(NEW_CFG, "list", "--stack", "debian,spack",
                    "--set", "debian.release=bookworm", "-t", "{kind}")
    assert res.exit_code == 0, res.output
    assert "debian" in res.output and "spack" in res.output


def test_list_old_paradigm_unaffected():
    res, _ = invoke(OLD_CFG, "list", "-i", "all", "-t", "{image}")
    assert res.exit_code == 0, res.output
    assert "debian:bookworm" in res.output
    assert "debian:trixie" in res.output


# --- dot --------------------------------------------------------------------

def test_dot_named_recipe():
    res, out = invoke(NEW_CFG, "dot", "phlex", "-o", "g.dot", write="g.dot")
    assert res.exit_code == 0, res.output
    assert "digraph" in out
    assert "localhost/winch/debian:" in out
    assert "localhost/winch/spack:" in out
    assert "->" in out                  # the chain edge


def test_dot_union_of_all_recipes():
    # No selector: union of phlex (debian->spack) and other (debian); the
    # shared debian node dedups so there are 2 distinct nodes.
    res, out = invoke(NEW_CFG, "dot", "-o", "g.dot", write="g.dot")
    assert res.exit_code == 0, res.output
    assert out.count("[label=") == 2


def test_dot_old_paradigm_unaffected():
    res, out = invoke(OLD_CFG, "dot", "-o", "g.dot", write="g.dot")
    assert res.exit_code == 0, res.output
    assert "digraph" in out


# --- render -----------------------------------------------------------------

def test_render_named_recipe_to_file():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(NEW_CFG)
        res = runner.invoke(cli, ["-c", "w.toml", "render", "phlex",
                                  "-T", "containerfile",
                                  "-o", "out/{kind}.dockerfile"])
        assert res.exit_code == 0, res.output
        spack = open("out/spack.dockerfile").read()
        assert spack.startswith("FROM localhost/winch/debian:")
        assert "RUN spack v1.1.0" in spack


def test_render_anonymous_stack_literal_template():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(NEW_CFG)
        res = runner.invoke(cli, ["-c", "w.toml", "render",
                                  "--stack", "debian,spack",
                                  "-t", "{kind}={image}",
                                  "-o", "out/{kind}.txt"])
        assert res.exit_code == 0, res.output
        assert open("out/spack.txt").read().startswith("spack=localhost/winch/spack:")


def test_render_requires_template():
    res, _ = invoke(NEW_CFG, "render", "phlex")
    assert res.exit_code != 0
