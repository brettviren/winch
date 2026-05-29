#!/usr/bin/env pytest
'''
Test winch.recipe capability (provides/requires) validation
(see doc/winch2-plan.md section 12.6).
'''

import tomllib
import pytest

from winch.config import parse, ConfigError
from winch.recipe import resolve, validate_capabilities


def load(text):
    _, layers, recipes = parse(tomllib.loads(text))
    return layers, recipes


def check(text, name="r", **kw):
    layers, recipes = load(text)
    rr = resolve(layers, recipes, name=name, **kw)
    return validate_capabilities(layers, rr)


# --- happy paths ------------------------------------------------------------

def test_compatible_stack_passes():
    avail = check("""
    [layer.debian]
    provides = ["os:debian"]
    containerfile = "FROM debian\\n"
    [layer.tools]
    provides = ["tools"]
    requires = ["os:debian"]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["debian", "tools"]
    """)
    assert avail == {"os:debian", "tools"}


def test_or_alternation_satisfied():
    # alma base satisfies an "os:debian|os:alma" requirement.
    avail = check("""
    [layer.alma]
    provides = ["os:alma"]
    containerfile = "FROM alma\\n"
    [layer.spack]
    requires = ["os:debian|os:alma"]
    provides = ["spack"]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["alma", "spack"]
    """)
    assert avail == {"os:alma", "spack"}


def test_multi_entry_and_satisfied():
    avail = check("""
    [layer.base]
    provides = ["os:debian", "py3"]
    containerfile = "FROM x\\n"
    [layer.app]
    requires = ["os:debian", "py3"]
    provides = ["app"]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["base", "app"]
    """)
    assert avail == {"os:debian", "py3", "app"}


def test_no_requires_composes_onto_anything():
    avail = check("""
    [layer.base]
    containerfile = "FROM x\\n"
    [layer.free]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["base", "free"]
    """)
    assert avail == set()


# --- failures ---------------------------------------------------------------

def test_missing_requirement_fails():
    with pytest.raises(ConfigError) as ei:
        check("""
        [layer.alma]
        provides = ["os:alma"]
        containerfile = "FROM alma\\n"
        [layer.apt]
        requires = ["os:debian"]
        body = "RUN apt-get install -y x\\n"
        [recipe.r]
        stack = ["alma", "apt"]
        """)
    msg = str(ei.value)
    assert 'layer "apt"' in msg
    assert "os:debian" in msg
    assert "os:alma" in msg          # available set is shown


def test_or_alternation_unsatisfied_fails():
    with pytest.raises(ConfigError) as ei:
        check("""
        [layer.fedora]
        provides = ["os:fedora"]
        containerfile = "FROM fedora\\n"
        [layer.spack]
        requires = ["os:debian|os:alma"]
        body = "RUN true\\n"
        [recipe.r]
        stack = ["fedora", "spack"]
        """)
    assert "os:debian|os:alma" in str(ei.value)


def test_multi_entry_partial_fails():
    with pytest.raises(ConfigError) as ei:
        check("""
        [layer.base]
        provides = ["os:debian"]
        containerfile = "FROM x\\n"
        [layer.app]
        requires = ["os:debian", "py3"]
        body = "RUN true\\n"
        [recipe.r]
        stack = ["base", "app"]
        """)
    # py3 is the unmet one.
    assert "py3" in str(ei.value)


def test_layer_cannot_satisfy_own_requirement():
    # provides added only AFTER requires checked.
    with pytest.raises(ConfigError):
        check("""
        [layer.base]
        containerfile = "FROM x\\n"
        [layer.self]
        requires = ["cap"]
        provides = ["cap"]
        body = "RUN true\\n"
        [recipe.r]
        stack = ["base", "self"]
        """)


def test_order_matters_requirer_before_provider_fails():
    with pytest.raises(ConfigError):
        check("""
        [layer.base]
        containerfile = "FROM x\\n"
        [layer.needer]
        requires = ["spack"]
        body = "RUN need\\n"
        [layer.provider]
        provides = ["spack"]
        body = "RUN provide\\n"
        [recipe.r]
        stack = ["base", "needer", "provider"]
        """)


# --- self-format of capabilities --------------------------------------------

def test_provides_uses_layer_var():
    avail = check("""
    [layer.gcc]
    version = "14"
    provides = ["pkg:gcc@{version}"]
    containerfile = "FROM x\\n"
    [recipe.r]
    stack = ["gcc"]
    """)
    assert "pkg:gcc@14" in avail


def test_requires_uses_layer_var_and_matches():
    avail = check("""
    [layer.gcc]
    version = "14"
    provides = ["pkg:gcc@{version}"]
    containerfile = "FROM x\\n"
    [layer.app]
    gccver = "14"
    requires = ["pkg:gcc@{gccver}"]
    provides = ["app"]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["gcc", "app"]
    """)
    assert avail == {"pkg:gcc@14", "app"}


def test_requires_formatted_version_mismatch_fails():
    with pytest.raises(ConfigError) as ei:
        check("""
        [layer.gcc]
        version = "13"
        provides = ["pkg:gcc@{version}"]
        containerfile = "FROM x\\n"
        [layer.app]
        requires = ["pkg:gcc@14"]
        body = "RUN true\\n"
        [recipe.r]
        stack = ["gcc", "app"]
        """)
    assert "pkg:gcc@14" in str(ei.value)


def test_chained_var_in_capability():
    avail = check("""
    [layer.x]
    package = "wct"
    version = "0.1"
    spec = "{package}@{version}"
    provides = ["pkg:{spec}"]
    containerfile = "FROM x\\n"
    [recipe.r]
    stack = ["x"]
    """)
    assert "pkg:wct@0.1" in avail


# --- --set override affects capability matching -----------------------------

def test_set_override_changes_capability():
    from winch.recipe import parse_set_overrides
    avail = check("""
    [layer.gcc]
    version = "13"
    provides = ["pkg:gcc@{version}"]
    containerfile = "FROM x\\n"
    [recipe.r]
    stack = ["gcc"]
    """, sets=parse_set_overrides(["gcc.version=14"]))
    assert "pkg:gcc@14" in avail
