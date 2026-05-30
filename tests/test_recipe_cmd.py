#!/usr/bin/env pytest
'''
Test the "winch recipe" build command (see doc/winch2-plan.md sections
12.5, 12.9).  podman is stubbed out via monkeypatch so no binary is needed.
'''

import pytest
from click.testing import CliRunner

import winch.cli as cli_mod
from winch.cli import cli


NEW_CFG = """
[layer.debian]
release = "bookworm"
provides = ["os:debian"]
containerfile = "FROM debian:{release}\\n"

[layer.spack]
version = "v1.1.0"
provides = ["spack", "pkg:gcc@{version}"]
requires = ["os:debian|os:alma"]
body = "RUN spack {version}\\n"

[recipe.r]
stack = ["debian", "spack"]
"""

OLD_CFG = """
[debian]
release = "bookworm"
image = "debian:{release}"
"""

INCOMPAT_CFG = """
[layer.alma]
provides = ["os:alma"]
containerfile = "FROM almalinux:9\\n"
[layer.apt]
requires = ["os:debian"]
body = "RUN apt-get install -y emacs\\n"
[recipe.bad]
stack = ["alma", "apt"]
"""


@pytest.fixture
def stub_podman(monkeypatch):
    '''Record build_image calls; pretend no image exists.'''
    calls = []
    monkeypatch.setattr(cli_mod, "image_exists", lambda name: False)
    monkeypatch.setattr(cli_mod, "remove_image", lambda name: True)
    monkeypatch.setattr(cli_mod, "build_image",
                        lambda image, cpath, *a: calls.append((image, str(cpath), list(a))))
    return calls


def run(cfg, *argv):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(cfg)
        res = runner.invoke(cli, ["-c", "w.toml", "recipe", *argv])
        # read any written Containerfiles back before the tempdir vanishes
        return res


# --- successful named-recipe build ------------------------------------------

def test_named_recipe_builds_in_order(stub_podman):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(NEW_CFG)
        res = runner.invoke(cli, ["-c", "w.toml", "recipe", "r"])
        assert res.exit_code == 0, res.output
        assert len(stub_podman) == 2
        debian_img, spack_img = stub_podman[0][0], stub_podman[1][0]
        assert debian_img.startswith("localhost/winch/debian:")
        assert spack_img.startswith("localhost/winch/spack:")

        # FROM injection: spack's Containerfile FROMs the debian image.
        spack_cpath = stub_podman[1][1]
        content = open(spack_cpath).read()
        assert content.startswith(f"FROM {debian_img}\n")
        assert "RUN spack v1.1.0" in content


def test_build_passes_winch_labels(stub_podman):
    res = run(NEW_CFG, "r")
    assert res.exit_code == 0, res.output
    spack_args = stub_podman[1][2]
    joined = " ".join(spack_args)
    assert "--label" in spack_args
    assert "winch.layer=spack" in joined
    assert "winch.var.version=v1.1.0" in joined
    assert "winch.provides=spack,pkg:gcc@v1.1.0" in joined
    # the digest label is the full inode
    assert "winch.digest=" in joined


# --- anonymous --stack and --set --------------------------------------------

def test_anonymous_stack_with_set(stub_podman):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("w.toml", "w") as f:
            f.write(NEW_CFG)
        res = runner.invoke(cli, ["-c", "w.toml", "recipe",
                                  "--stack", "debian,spack",
                                  "--set", "debian.release=trixie",
                                  "--set", "spack.version=v9"])
        assert res.exit_code == 0, res.output
        assert len(stub_podman) == 2
        debian_content = open(stub_podman[0][1]).read()
        spack_content = open(stub_podman[1][1]).read()
        assert "FROM debian:trixie" in debian_content
        assert "RUN spack v9" in spack_content


# --- error paths (must abort before any build) ------------------------------

def test_old_config_rejected(stub_podman):
    res = run(OLD_CFG, "r")
    assert res.exit_code != 0
    assert "new-paradigm" in res.output
    assert stub_podman == []


def test_name_and_stack_conflict(stub_podman):
    res = run(NEW_CFG, "r", "--stack", "debian")
    assert res.exit_code != 0
    assert "either" in res.output.lower()
    assert stub_podman == []


def test_neither_name_nor_stack(stub_podman):
    res = run(NEW_CFG)
    assert res.exit_code != 0
    assert stub_podman == []


def test_unknown_recipe(stub_podman):
    res = run(NEW_CFG, "nope")
    assert res.exit_code != 0
    assert "nope" in res.output
    assert stub_podman == []


def test_capability_failure_aborts_before_build(stub_podman):
    res = run(INCOMPAT_CFG, "bad")
    assert res.exit_code != 0
    assert 'layer "apt"' in res.output
    assert "os:debian" in res.output
    # nothing built
    assert stub_podman == []


# --- rebuild / force / empty branches ---------------------------------------

def test_empty_recipe_warns_no_build(stub_podman):
    cfg = NEW_CFG + "\n[recipe.empty]\nstack = []\n"
    res = run(cfg, "empty")
    assert res.exit_code == 0, res.output
    assert "no layers to build" in res.output
    assert stub_podman == []


def test_rebuild_none_skips_existing(stub_podman, monkeypatch):
    monkeypatch.setattr(cli_mod, "image_exists", lambda name: True)
    res = run(NEW_CFG, "r", "-r", "none")
    assert res.exit_code == 0, res.output
    assert "not rebuilding existing image" in res.output
    assert stub_podman == []


def test_force_removes_existing_and_no_cache(stub_podman, monkeypatch):
    removed = []
    monkeypatch.setattr(cli_mod, "image_exists", lambda name: True)
    monkeypatch.setattr(cli_mod, "remove_image",
                        lambda name: removed.append(name) or True)
    res = run(NEW_CFG, "r", "-f", "all")
    assert res.exit_code == 0, res.output
    assert len(removed) == 2                 # both layers force-removed
    assert len(stub_podman) == 2
    for _image, _cpath, a in stub_podman:
        assert "--no-cache" in a
