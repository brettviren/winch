#!/usr/bin/env pytest
'''
Tests for TOML include resolution and config search-path handling
(config.build_search_path / resolve_config_path / load_config).
'''

import pytest

from winch.config import (build_search_path, resolve_config_path, load_config,
                          ConfigError)


# --- search-path assembly ---------------------------------------------------

def test_build_search_path_order_and_split():
    # -p values (flag order), each ":"-split, then WINCH_PATH appended.
    dirs = build_search_path(["/a:/b", "/c"], env_path="/d:/e")
    assert dirs == ["/a", "/b", "/c", "/d", "/e"]


def test_build_search_path_empty():
    assert build_search_path([], env_path=None) == []
    assert build_search_path([], env_path="") == []


# --- path resolution --------------------------------------------------------

def test_absolute_resolves_to_itself(tmp_path):
    f = tmp_path / "x.toml"; f.write_text("")
    assert resolve_config_path(str(f)) == f


def test_absolute_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_config_path(str(tmp_path / "nope.toml"))


def test_cwd_beats_search(tmp_path, monkeypatch):
    # Same basename in CWD and in a search dir: CWD wins.
    cwd = tmp_path / "cwd"; cwd.mkdir()
    sp = tmp_path / "sp"; sp.mkdir()
    (cwd / "f.toml").write_text("# cwd")
    (sp / "f.toml").write_text("# sp")
    monkeypatch.chdir(cwd)
    got = resolve_config_path("f.toml", context=None, search=[str(sp)])
    assert got == cwd / "f.toml"


def test_context_beats_search(tmp_path, monkeypatch):
    # Not in CWD; present in both context and search -> context wins.
    cwd = tmp_path / "cwd"; cwd.mkdir()
    ctx = tmp_path / "ctx"; ctx.mkdir()
    sp = tmp_path / "sp"; sp.mkdir()
    (ctx / "f.toml").write_text("# ctx")
    (sp / "f.toml").write_text("# sp")
    monkeypatch.chdir(cwd)
    got = resolve_config_path("f.toml", context=str(ctx), search=[str(sp)])
    assert got == ctx / "f.toml"


def test_search_used_last(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"; cwd.mkdir()
    sp = tmp_path / "sp"; sp.mkdir()
    (sp / "f.toml").write_text("# sp")
    monkeypatch.chdir(cwd)
    got = resolve_config_path("f.toml", context=None, search=[str(sp)])
    assert got == sp / "f.toml"


# --- include loading --------------------------------------------------------

def test_include_string_and_precedence(tmp_path):
    # base provides a value; the including file overrides it (own wins).
    (tmp_path / "base.toml").write_text('[layer.x]\nrelease = "a"\nkeep = "b"\n')
    (tmp_path / "top.toml").write_text(
        '[winch]\ninclude = "base.toml"\n[layer.x]\nrelease = "z"\n')
    cfg = load_config([str(tmp_path / "top.toml")])
    assert cfg["layer"]["x"]["release"] == "z"   # own wins
    assert cfg["layer"]["x"]["keep"] == "b"       # inherited from base
    assert "include" not in cfg.get("winch", {})  # directive stripped


def test_include_list_and_context(tmp_path):
    # top includes two siblings by bare name; they resolve via the context dir
    # (top's own directory), not CWD or search.
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "a.toml").write_text('[layer.a]\nv = 1\n')
    (sub / "b.toml").write_text('[layer.b]\nv = 2\n')
    (sub / "top.toml").write_text('[winch]\ninclude = ["a.toml", "b.toml"]\n')
    cfg = load_config([str(sub / "top.toml")])
    assert cfg["layer"]["a"]["v"] == 1
    assert cfg["layer"]["b"]["v"] == 2


def test_include_recursive(tmp_path):
    (tmp_path / "c.toml").write_text('[layer.c]\nv = 3\n')
    (tmp_path / "b.toml").write_text('[winch]\ninclude = "c.toml"\n[layer.b]\nv = 2\n')
    (tmp_path / "a.toml").write_text('[winch]\ninclude = "b.toml"\n[layer.a]\nv = 1\n')
    cfg = load_config([str(tmp_path / "a.toml")])
    assert set(cfg["layer"]) == {"a", "b", "c"}


def test_include_cycle_terminates(tmp_path):
    # a -> b -> a: the seen-set guard prevents infinite recursion.
    (tmp_path / "a.toml").write_text('[winch]\ninclude = "b.toml"\n[layer.a]\nv = 1\n')
    (tmp_path / "b.toml").write_text('[winch]\ninclude = "a.toml"\n[layer.b]\nv = 2\n')
    cfg = load_config([str(tmp_path / "a.toml")])
    assert set(cfg["layer"]) == {"a", "b"}


def test_include_diamond_no_double_append(tmp_path):
    # a includes b and c; both include d.  d's list-valued field must not be
    # appended twice (the seen-set loads d only once).
    (tmp_path / "d.toml").write_text('[layer.d]\nprovides = ["one"]\n')
    (tmp_path / "b.toml").write_text('[winch]\ninclude = "d.toml"\n')
    (tmp_path / "c.toml").write_text('[winch]\ninclude = "d.toml"\n')
    (tmp_path / "a.toml").write_text('[winch]\ninclude = ["b.toml", "c.toml"]\n')
    cfg = load_config([str(tmp_path / "a.toml")])
    assert cfg["layer"]["d"]["provides"] == ["one"]


def test_missing_include_fails_fast(tmp_path):
    (tmp_path / "top.toml").write_text('[winch]\ninclude = "ghost.toml"\n')
    with pytest.raises(FileNotFoundError):
        load_config([str(tmp_path / "top.toml")])


def test_include_via_search_path(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "base.toml").write_text('[layer.x]\nv = 1\n')
    work = tmp_path / "work"; work.mkdir()
    (work / "top.toml").write_text('[winch]\ninclude = "base.toml"\n')
    monkeypatch.chdir(work)
    cfg = load_config(["top.toml"], search=[str(lib)])
    assert cfg["layer"]["x"]["v"] == 1


def test_no_config_returns_none(tmp_path, monkeypatch):
    # No -c and no default winch.toml under a redirected config dir -> tolerated.
    monkeypatch.setenv("XDG_CONFIG_DIR", str(tmp_path / "empty"))
    assert load_config([], search=[]) is None
