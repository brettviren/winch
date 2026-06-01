#!/usr/bin/env pytest
'''
Test cross-layer variable references: {layer.NAME.VAR} and {requires.NAME.VAR}.

These are pre-expanded in generate_instances before the normal self_format pass,
so they compose freely with {varname} and {parent[varname]} references.
'''

import tomllib
import pytest

from winch.config import parse, ConfigError
from winch.recipe import resolve
from winch.graph import generate_instances


def load(text):
    _, layers, recipes = parse(tomllib.loads(text))
    return layers, recipes


def gen(layers, recipes, **kw):
    rr = resolve(layers, recipes, **kw)
    return generate_instances(layers, [rr])


def datas(graph):
    '''Return I-node data dicts in stack (topological) order.'''
    leaves = [n for n in graph.I.nodes() if graph.I.out_degree(n) == 0]
    assert len(leaves) == 1
    path = graph.ipath(leaves[0])
    return [graph.data(n) for n in path]


# ---------------------------------------------------------------------------
# {layer.NAME.VAR} — explicit named-layer reference
# ---------------------------------------------------------------------------

def test_layer_ref_basic():
    layers, recipes = load("""
    [layer.base]
    release = "stable"
    containerfile = "FROM scratch\\n"

    [layer.top]
    body = "RUN setup --os {layer.base.release}\\n"

    [recipe.r]
    stack = ["base", "top"]
    """)
    g = gen(layers, recipes, name="r")
    _, top = datas(g)
    assert "RUN setup --os stable" in top["containerfile"]


def test_layer_ref_skips_one_level():
    # Reference a layer two steps back (not just the direct parent).
    layers, recipes = load("""
    [layer.os]
    release = "bookworm"
    containerfile = "FROM debian:{release}\\n"

    [layer.middle]
    body = "RUN install tools\\n"

    [layer.app]
    body = "RUN setup --os {layer.os.release}\\n"

    [recipe.r]
    stack = ["os", "middle", "app"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, app = datas(g)
    assert "RUN setup --os bookworm" in app["containerfile"]


def test_layer_ref_in_variable_value():
    # Cross-layer ref in a var value, which is then used by {varname}.
    layers, recipes = load("""
    [layer.base]
    prefix = "pkg"
    containerfile = "FROM scratch\\n"

    [layer.top]
    suffix = "1.0"
    spec = "{layer.base.prefix}-{suffix}"
    body = "RUN install {spec}\\n"

    [recipe.r]
    stack = ["base", "top"]
    """)
    g = gen(layers, recipes, name="r")
    _, top = datas(g)
    assert "RUN install pkg-1.0" in top["containerfile"]
    assert top["spec"] == "pkg-1.0"


def test_layer_ref_with_recipe_override():
    # Recipe-level overrides on the referenced layer flow through correctly.
    layers, recipes = load("""
    [layer.base]
    release = "bookworm"
    containerfile = "FROM debian:{release}\\n"

    [layer.app]
    body = "RUN echo {layer.base.release}\\n"

    [recipe.r]
    stack = ["base", "app"]
    base.release = "trixie"
    """)
    g = gen(layers, recipes, name="r")
    _, app = datas(g)
    assert "RUN echo trixie" in app["containerfile"]


def test_layer_ref_unknown_layer_raises():
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.top]
    body = "RUN echo {layer.ghost.version}\\n"

    [recipe.r]
    stack = ["base", "top"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "ghost" in msg
    assert "not a prior layer" in msg


def test_layer_ref_future_layer_raises():
    # Referencing a layer that is later in the stack (not yet processed).
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.early]
    body = "RUN echo {layer.later.version}\\n"

    [layer.later]
    version = "2.0"
    body = "RUN true\\n"

    [recipe.r]
    stack = ["base", "early", "later"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    assert "later" in str(ei.value)
    assert "not a prior layer" in str(ei.value)


def test_layer_ref_unknown_variable_raises():
    layers, recipes = load("""
    [layer.base]
    release = "stable"
    containerfile = "FROM scratch\\n"

    [layer.top]
    body = "RUN echo {layer.base.nope}\\n"

    [recipe.r]
    stack = ["base", "top"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "nope" in msg
    assert 'no variable' in msg


# ---------------------------------------------------------------------------
# {requires.NAME.VAR} — indirect reference via requirement name
# ---------------------------------------------------------------------------

def test_requires_ref_basic():
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.compiler]
    version = "14"
    provides = ["gcc"]
    body = "RUN install gcc-{version}\\n"

    [layer.app]
    requires = ["gcc"]
    body = "RUN build --gcc {requires.gcc.version}\\n"

    [recipe.r]
    stack = ["base", "compiler", "app"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, app = datas(g)
    assert "RUN build --gcc 14" in app["containerfile"]


def test_requires_ref_uses_resolved_vars():
    # The var value in the providing layer is the fully resolved value.
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.compiler]
    major = "14"
    minor = "2"
    version = "{major}.{minor}"
    provides = ["gcc"]
    body = "RUN install gcc-{version}\\n"

    [layer.app]
    requires = ["gcc"]
    body = "RUN build --gcc {requires.gcc.version}\\n"

    [recipe.r]
    stack = ["base", "compiler", "app"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, app = datas(g)
    assert "RUN build --gcc 14.2" in app["containerfile"]


def test_requires_ref_last_wins():
    # When multiple prior layers provide the same capability, the one latest
    # in the stack (closest to the current layer) wins.
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.gcc13]
    version = "13"
    provides = ["gcc"]
    body = "RUN install gcc-{version}\\n"

    [layer.gcc14]
    version = "14"
    provides = ["gcc"]
    body = "RUN install gcc-{version}\\n"

    [layer.app]
    requires = ["gcc"]
    body = "RUN build --gcc {requires.gcc.version}\\n"

    [recipe.r]
    stack = ["base", "gcc13", "gcc14", "app"]
    """)
    g = gen(layers, recipes, name="r")
    *_, app = datas(g)
    assert "RUN build --gcc 14" in app["containerfile"]


def test_requires_ref_in_var_value():
    # Cross-requires ref used in a variable that is itself used via {varname}.
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.pkg]
    name = "spack"
    provides = ["spack"]
    body = "RUN install spack\\n"

    [layer.app]
    requires = ["spack"]
    tool = "{requires.spack.name}"
    body = "RUN {tool} install\\n"

    [recipe.r]
    stack = ["base", "pkg", "app"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, app = datas(g)
    assert "RUN spack install" in app["containerfile"]
    assert app["tool"] == "spack"


def test_requires_ref_not_in_requires_raises():
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.compiler]
    version = "14"
    provides = ["gcc"]
    body = "RUN install gcc\\n"

    [layer.app]
    body = "RUN build --gcc {requires.gcc.version}\\n"

    [recipe.r]
    stack = ["base", "compiler", "app"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "gcc" in msg
    assert "not in this layer" in msg


def test_requires_ref_no_provider_raises():
    # The requirement is listed but no prior layer provides it.
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.app]
    requires = ["spack"]
    body = "RUN {requires.spack.version}\\n"

    [recipe.r]
    stack = ["base", "app"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "spack" in msg
    assert "no prior layer provides" in msg


def test_requires_ref_unknown_variable_raises():
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"

    [layer.pkg]
    version = "1.0"
    provides = ["spack"]
    body = "RUN install spack\\n"

    [layer.app]
    requires = ["spack"]
    body = "RUN {requires.spack.nope}\\n"

    [recipe.r]
    stack = ["base", "pkg", "app"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "nope" in msg
    assert "no variable" in msg


# ---------------------------------------------------------------------------
# Mixing both new forms with existing forms
# ---------------------------------------------------------------------------

def test_mixed_refs_in_one_string():
    layers, recipes = load("""
    [layer.base]
    os = "debian"
    containerfile = "FROM scratch\\n"

    [layer.compiler]
    version = "14"
    provides = ["gcc"]
    body = "RUN install gcc\\n"

    [layer.app]
    build_id = "myapp"
    requires = ["gcc"]
    body = "RUN build {build_id} os={layer.base.os} gcc={requires.gcc.version}\\n"

    [recipe.r]
    stack = ["base", "compiler", "app"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, app = datas(g)
    assert "RUN build myapp os=debian gcc=14" in app["containerfile"]


def test_layer_ref_combined_with_parent_ref():
    # {layer.X.V} and {parent[V]} can coexist.
    layers, recipes = load("""
    [layer.base]
    tag = "slim"
    containerfile = "FROM debian:{tag}\\n"

    [layer.mid]
    body = "RUN setup\\n"

    [layer.top]
    body = "RUN echo base={layer.base.tag} parent={parent[kind]}\\n"

    [recipe.r]
    stack = ["base", "mid", "top"]
    """)
    g = gen(layers, recipes, name="r")
    _, _, top = datas(g)
    assert "base=slim" in top["containerfile"]
    assert "parent=mid" in top["containerfile"]
