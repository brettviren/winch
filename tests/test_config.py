#!/usr/bin/env pytest
'''
Test winch.config new-paradigm detection and parsing (see doc/winch2-plan.md
section 12).
'''

import tomllib
import pytest

from winch.config import (
    parse, detect_paradigm, parse_layer, parse_recipe, ConfigError,
)


def from_toml(text):
    return tomllib.loads(text)


# --- paradigm detection -----------------------------------------------------

def test_detect_old():
    cfg = from_toml("""
    [debian]
    release = ['bookworm', 'trixie']
    image = '{kind}:{release}'

    [debian_minimal]
    parent_kind = 'debian'
    """)
    assert detect_paradigm(cfg) == "old"
    paradigm, layers, recipes = parse(cfg)
    assert paradigm == "old"
    assert layers == {} and recipes == {}


def test_detect_new_layer_only():
    cfg = from_toml("""
    [layer.debian]
    containerfile = "FROM debian:{release}\\n"
    """)
    assert detect_paradigm(cfg) == "new"


def test_detect_new_recipe_only():
    cfg = from_toml("""
    [recipe.foo]
    stack = []
    """)
    assert detect_paradigm(cfg) == "new"


def test_detect_empty_is_old():
    assert detect_paradigm({}) == "old"
    assert parse({}) == ("old", {}, {})


def test_winch_options_table_allowed_with_new():
    cfg = from_toml("""
    [winch]
    something = 'ok'

    [layer.debian]
    containerfile = "FROM debian\\n"
    """)
    # The reserved [winch] options table must not trigger a mixed-config error.
    assert detect_paradigm(cfg) == "new"


def test_mixed_config_is_error():
    cfg = from_toml("""
    [layer.spack]
    body = "RUN true\\n"

    [debian]
    parent_kind = 'whatever'
    """)
    with pytest.raises(ConfigError) as ei:
        detect_paradigm(cfg)
    assert "debian" in str(ei.value)


# --- layer parsing ----------------------------------------------------------

def test_parse_layer_full():
    cfg = from_toml("""
    [layer.spack]
    version = "v1.1.0"
    provides = ["spack"]
    requires = ["os:debian|os:alma"]
    body = "RUN clone {version}\\n"
    """)
    _, layers, _ = parse(cfg)
    spack = layers["spack"]
    assert spack.vars == {"version": "v1.1.0"}
    assert spack.provides == ["spack"]
    assert spack.requires == ["os:debian|os:alma"]
    assert spack.body == "RUN clone {version}\n"
    assert spack.containerfile is None


def test_parse_layer_base_via_containerfile():
    cfg = from_toml("""
    [layer.debian]
    provides = ["os:debian"]
    containerfile = "FROM debian:{release}\\n"
    """)
    _, layers, _ = parse(cfg)
    deb = layers["debian"]
    assert deb.containerfile == "FROM debian:{release}\n"
    assert deb.body is None
    assert deb.provides == ["os:debian"]


def test_layer_provides_single_string_normalized():
    layer = parse_layer("x", {"provides": "spack"})
    assert layer.provides == ["spack"]


def test_layer_rejects_parent_kind():
    with pytest.raises(ConfigError):
        parse_layer("x", {"parent_kind": "debian"})


def test_layer_rejects_list_variable():
    # A non-capability list value is an error (no variants in new paradigm).
    with pytest.raises(ConfigError) as ei:
        parse_layer("x", {"release": ["bookworm", "trixie"]})
    assert "release" in str(ei.value)


def test_layer_scalar_variable_types():
    layer = parse_layer("x", {"version": 14, "flag": True, "pi": 3.14})
    assert layer.vars == {"version": 14, "flag": True, "pi": 3.14}


def test_layer_provides_bad_element():
    with pytest.raises(ConfigError):
        parse_layer("x", {"provides": ["ok", 5]})


# --- recipe parsing ---------------------------------------------------------

def test_parse_recipe_full():
    cfg = from_toml("""
    [layer.debian_base]
    body = "RUN true\\n"
    [layer.spack]
    body = "RUN true\\n"
    [layer.base]
    containerfile = "FROM x\\n"

    [recipe.base-r]
    stack = ["base"]

    [recipe.phlex]
    recipe_base = "base-r"
    stack = ["debian_base", "spack"]
    spack.version = "v1.1.0"
    debian_base.foo = "bar"
    """)
    _, _, recipes = parse(cfg)
    phlex = recipes["phlex"]
    assert phlex.recipe_base == ["base-r"]
    assert phlex.stack == ["debian_base", "spack"]
    assert phlex.layer_vars == {
        "spack": {"version": "v1.1.0"},
        "debian_base": {"foo": "bar"},
    }


def test_recipe_dotted_and_subtable_uniform():
    # "spack.version" and a [recipe.r.spack] subtable nest identically.
    a = parse_recipe("r", from_toml('stack=["spack"]\nspack.version = "v1"'))
    b = parse_recipe("r", from_toml('stack=["spack"]\n[spack]\nversion = "v1"'))
    assert a.layer_vars == b.layer_vars == {"spack": {"version": "v1"}}


def test_recipe_base_string_normalized():
    r = parse_recipe("r", {"recipe_base": "other", "stack": []})
    assert r.recipe_base == ["other"]


def test_recipe_default_empty_stack():
    r = parse_recipe("r", {})
    assert r.stack == [] and r.recipe_base == [] and r.layer_vars == {}


def test_recipe_bad_scalar_key_is_error():
    # A bare scalar (not LAYER.VAR) in a recipe is ambiguous -> error.
    with pytest.raises(ConfigError) as ei:
        parse_recipe("r", {"foo": "bar"})
    assert "foo" in str(ei.value)


def test_recipe_stack_must_be_list():
    with pytest.raises(ConfigError):
        parse_recipe("r", {"stack": "debian"})


# --- cross-references -------------------------------------------------------

def test_recipe_unknown_layer_is_error():
    cfg = from_toml("""
    [layer.debian]
    containerfile = "FROM x\\n"
    [recipe.r]
    stack = ["nope"]
    """)
    with pytest.raises(ConfigError) as ei:
        parse(cfg)
    assert "nope" in str(ei.value)


def test_recipe_unknown_base_is_error():
    cfg = from_toml("""
    [layer.debian]
    containerfile = "FROM x\\n"
    [recipe.r]
    recipe_base = "missing"
    stack = ["debian"]
    """)
    with pytest.raises(ConfigError) as ei:
        parse(cfg)
    assert "missing" in str(ei.value)


def test_recipe_unknown_layer_var_target_is_error():
    cfg = from_toml("""
    [layer.debian]
    containerfile = "FROM x\\n"
    [recipe.r]
    stack = ["debian"]
    ghost.var = "x"
    """)
    with pytest.raises(ConfigError) as ei:
        parse(cfg)
    assert "ghost" in str(ei.value)
