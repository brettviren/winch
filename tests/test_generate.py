#!/usr/bin/env pytest
'''
Test winch.graph new-paradigm instance-chain generation
(see doc/winch2-plan.md sections 12.2, 12.7).
'''

import re
import tomllib
import pytest

from winch.config import parse, ConfigError
from winch.recipe import resolve, parse_set_overrides
from winch.graph import generate_instances, WINCH_IMAGE_PREFIX


def load(text):
    _, layers, recipes = parse(tomllib.loads(text))
    return layers, recipes


def gen(layers, recipes, **kw):
    rr = resolve(layers, recipes, **kw)
    return generate_instances(layers, [rr])


# A small but representative new-paradigm config.
CFG = """
[layer.debian]
release = "bookworm"
provides = ["os:debian"]
containerfile = "FROM debian:{release}\\n"

[layer.tools]
provides = ["tools"]
requires = ["os:debian|os:alma"]
body = "RUN apt-get install -y emacs\\n"

[layer.spack]
version = "v1.1.0"
body = "RUN spack {version}\\n"

[recipe.r]
stack = ["debian", "tools", "spack"]
"""


def inodes_in_order(graph):
    '''Return I-node ids in stack (topological) order.'''
    leaves = [n for n in graph.I.nodes() if graph.I.out_degree(n) == 0]
    assert len(leaves) == 1
    return graph.ipath(leaves[0])


def datas_in_order(graph):
    return [graph.data(n) for n in inodes_in_order(graph)]


# --- chain shape ------------------------------------------------------------

def test_chain_order_and_length():
    layers, recipes = load(CFG)
    g = gen(layers, recipes, name="r")
    datas = datas_in_order(g)
    assert [d["kind"] for d in datas] == ["debian", "tools", "spack"]


def test_from_injection_for_body():
    layers, recipes = load(CFG)
    g = gen(layers, recipes, name="r")
    datas = datas_in_order(g)
    debian, tools, spack = datas
    # The base used the containerfile escape hatch verbatim.
    assert debian["containerfile"] == "FROM debian:bookworm\n"
    # body layers get FROM injected, referencing the parent's resolved image.
    assert tools["containerfile"].startswith(f"FROM {debian['image']}\n")
    assert spack["containerfile"].startswith(f"FROM {tools['image']}\n")
    assert "RUN spack v1.1.0" in spack["containerfile"]


def test_parent_interpolation():
    layers, recipes = load(CFG)
    g = gen(layers, recipes, name="r")
    debian, tools, spack = datas_in_order(g)
    # {parent[image]} resolved to the concrete parent image string.
    assert "{parent" not in tools["containerfile"]
    assert tools["parent"]["image"] == debian["image"]


# --- image naming -----------------------------------------------------------

def test_digest_image_names():
    layers, recipes = load(CFG)
    g = gen(layers, recipes, name="r")
    pat = re.compile(rf"^{re.escape(WINCH_IMAGE_PREFIX)}/(debian|tools|spack):[0-9a-f]{{12}}$")
    for d in datas_in_order(g):
        assert pat.match(d["image"]), d["image"]


def test_deterministic_across_runs():
    layers, recipes = load(CFG)
    g1 = gen(layers, recipes, name="r")
    g2 = gen(layers, recipes, name="r")
    assert sorted(g1.I.nodes()) == sorted(g2.I.nodes())
    assert {d["image"] for _, d in g1.I.nodes.data()} == \
           {d["image"] for _, d in g2.I.nodes.data()}


def test_explicit_image_override():
    layers, recipes = load("""
    [layer.debian]
    release = "bookworm"
    image = "my/custom:tag"
    containerfile = "FROM debian:{release}\\n"
    [recipe.r]
    stack = ["debian"]
    """)
    g = gen(layers, recipes, name="r")
    (d,) = datas_in_order(g)
    assert d["image"] == "my/custom:tag"


# --- prefix dedup -----------------------------------------------------------

def test_prefix_dedup_across_recipes():
    layers, recipes = load("""
    [layer.debian]
    release = "bookworm"
    containerfile = "FROM debian:{release}\\n"
    [layer.a]
    body = "RUN a\\n"
    [layer.b]
    body = "RUN b\\n"
    [recipe.ra]
    stack = ["debian", "a"]
    [recipe.rb]
    stack = ["debian", "b"]
    """)
    ra = resolve(layers, recipes, name="ra")
    rb = resolve(layers, recipes, name="rb")
    g = generate_instances(layers, [ra, rb])
    # debian + a + b = 3 distinct nodes (shared debian prefix deduped).
    assert g.I.number_of_nodes() == 3
    # The shared debian node has two children (a and b).
    roots = [n for n in g.I.nodes() if g.I.in_degree(n) == 0]
    assert len(roots) == 1
    assert g.I.out_degree(roots[0]) == 2


def test_variant_difference_does_not_dedup():
    # Same layers, different parent variable -> different digest, no dedup.
    layers, recipes = load("""
    [layer.debian]
    release = "bookworm"
    containerfile = "FROM debian:{release}\\n"
    [layer.a]
    body = "RUN a\\n"
    [recipe.r1]
    stack = ["debian", "a"]
    [recipe.r2]
    stack = ["debian", "a"]
    debian.release = "trixie"
    """)
    r1 = resolve(layers, recipes, name="r1")
    r2 = resolve(layers, recipes, name="r2")
    g = generate_instances(layers, [r1, r2])
    # Two distinct debian nodes (different release) each with its own 'a' child.
    assert g.I.number_of_nodes() == 4


# --- resolution check -------------------------------------------------------

def test_unresolved_markup_errors():
    layers, recipes = load("""
    [layer.x]
    body = "RUN echo {nope}\\n"
    [layer.base]
    containerfile = "FROM scratch\\n"
    [recipe.r]
    stack = ["base", "x"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    msg = str(ei.value)
    assert "x" in msg and "nope" in msg


def test_base_body_without_parent_is_unresolved():
    # A base layer using body (no parent) leaves {parent[image]} unresolved.
    layers, recipes = load("""
    [layer.base]
    body = "RUN true\\n"
    [recipe.r]
    stack = ["base"]
    """)
    with pytest.raises(ConfigError) as ei:
        gen(layers, recipes, name="r")
    assert "parent" in str(ei.value)


def test_escaped_braces_are_not_unresolved():
    # A shell ${VAR} written with escaped braces must survive without error.
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"
    [layer.x]
    body = "RUN echo ${{HOME}} and {{literal}}\\n"
    [recipe.r]
    stack = ["base", "x"]
    """)
    g = gen(layers, recipes, name="r")
    _, x = datas_in_order(g)
    assert "${HOME}" in x["containerfile"]
    assert "{literal}" in x["containerfile"]


def test_chained_layer_vars_resolve():
    layers, recipes = load("""
    [layer.base]
    containerfile = "FROM scratch\\n"
    [layer.x]
    package = "wire-cell-toolkit"
    version = "0.1"
    spec = "{package}@{version}"
    body = "RUN install {spec}\\n"
    [recipe.r]
    stack = ["base", "x"]
    """)
    g = gen(layers, recipes, name="r")
    _, x = datas_in_order(g)
    assert "RUN install wire-cell-toolkit@0.1" in x["containerfile"]


# --- --set flows through to generation --------------------------------------

def test_set_changes_generated_content():
    layers, recipes = load(CFG)
    sets = parse_set_overrides(["spack.version=v9.9.9"])
    g = gen(layers, recipes, name="r", sets=sets)
    _, _, spack = datas_in_order(g)
    assert "RUN spack v9.9.9" in spack["containerfile"]
