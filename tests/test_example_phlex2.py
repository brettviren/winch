#!/usr/bin/env pytest
'''
Guard the shipped example/phlex2.toml: it must remain a valid new-paradigm
config whose phlex recipes resolve, validate and generate.  No podman needed.
'''

from pathlib import Path
import pytest

from winch.config import load_many, parse
from winch.recipe import resolve, validate_capabilities
from winch.graph import generate_instances

EXAMPLE = Path(__file__).resolve().parent.parent / "example" / "phlex2.toml"


@pytest.fixture(scope="module")
def parsed():
    paradigm, layers, recipes = parse(load_many(str(EXAMPLE)))
    assert paradigm == "new"
    return layers, recipes


def test_example_exists():
    assert EXAMPLE.exists()


@pytest.mark.parametrize("name,os_provide", [
    ("phlex-debian", "os:debian"),
    ("phlex-alma", "os:alma"),
])
def test_phlex_recipe_resolves_validates_generates(parsed, name, os_provide):
    layers, recipes = parsed
    rr = resolve(layers, recipes, name=name)

    # multi-base inheritance concatenates OS base + shared phlex stack
    assert rr.stack[-3:] == ["spack", "spack_gcc", "spack_phlex"]

    avail = validate_capabilities(layers, rr)
    assert os_provide in avail
    assert "spack" in avail
    assert "pkg:gcc@14" in avail        # formatted provides
    assert "phlex" in avail

    g = generate_instances(layers, [rr])
    # one node per stack entry, linear chain
    assert g.I.number_of_nodes() == len(rr.stack)
    leaves = [n for n in g.I.nodes() if g.I.out_degree(n) == 0]
    assert len(leaves) == 1
    chain = g.I.nodes[leaves[0]]
    assert chain["kind"] == "spack_phlex"
    # FROM injection chained the spack layer onto its OS-base parent
    spack = [d for _, d in g.I.nodes.data() if d["kind"] == "spack"][0]
    assert spack["containerfile"].startswith(f"FROM {spack['parent']['image']}\n")


def test_phlex_stack_is_partial_base(parsed):
    # phlex-stack has no OS root; resolving is fine but generating standalone
    # fails (its first body layer has no parent).  This is why the union view
    # (dot/render with no selector) skips it.
    from winch.config import ConfigError
    layers, recipes = parsed
    rr = resolve(layers, recipes, name="phlex-stack")
    with pytest.raises(ConfigError):
        generate_instances(layers, [rr])
