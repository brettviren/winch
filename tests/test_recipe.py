#!/usr/bin/env pytest
'''
Test winch.recipe resolution and recipe_base inheritance
(see doc/winch2-plan.md sections 12.3-12.5).
'''

import tomllib
import pytest

from winch.config import parse, ConfigError, Layer, Recipe
from winch.recipe import resolve, parse_set_overrides


def cfg(text):
    '''Parse TOML text and return (layers, recipes).'''
    _, layers, recipes = parse(tomllib.loads(text))
    return layers, recipes


LAYERS_TOML = """
[layer.debian]
release = "bookworm"
containerfile = "FROM debian:{release}\\n"
[layer.base]
pkgs = "default"
body = "RUN install {pkgs}\\n"
[layer.spack]
version = "v1.0.0"
body = "RUN spack {version}\\n"
[layer.gcc]
version = "13"
body = "RUN gcc {version}\\n"
"""


# --- basic resolution -------------------------------------------------------

def test_resolve_named_simple():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.r]
    stack = ["debian", "base"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.name == "r"
    assert rr.stack == ["debian", "base"]
    # defaults flow through (lowest precedence)
    assert rr.layer_vars["debian"] == {"release": "bookworm"}
    assert rr.layer_vars["base"] == {"pkgs": "default"}


def test_recipe_level_override_beats_default():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.r]
    stack = ["debian"]
    debian.release = "trixie"
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.layer_vars["debian"] == {"release": "trixie"}


# --- recipe_base inheritance ------------------------------------------------

def test_single_base_stack_concatenation():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.basebox]
    stack = ["debian"]
    [recipe.r]
    recipe_base = "basebox"
    stack = ["base", "spack"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.stack == ["debian", "base", "spack"]


def test_multiple_bases_in_order():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["debian"]
    [recipe.b]
    stack = ["base"]
    [recipe.r]
    recipe_base = ["a", "b"]
    stack = ["spack"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.stack == ["debian", "base", "spack"]


def test_nested_base_resolution():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["debian"]
    [recipe.b]
    recipe_base = "a"
    stack = ["base"]
    [recipe.r]
    recipe_base = "b"
    stack = ["spack"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.stack == ["debian", "base", "spack"]


def test_var_last_wins_across_bases():
    # base 'a' sets spack.version, base 'b' overrides it, recipe overrides again.
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["spack"]
    spack.version = "from-a"
    [recipe.b]
    stack = []
    spack.version = "from-b"
    [recipe.r]
    recipe_base = ["a", "b"]
    spack.version = "from-r"
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.layer_vars["spack"]["version"] == "from-r"


def test_var_inherited_from_base_when_not_overridden():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["spack"]
    spack.version = "from-a"
    [recipe.r]
    recipe_base = "a"
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.layer_vars["spack"]["version"] == "from-a"


def test_earlier_base_loses_to_later_base():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["spack"]
    spack.version = "from-a"
    [recipe.b]
    stack = []
    spack.version = "from-b"
    [recipe.r]
    recipe_base = ["a", "b"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.layer_vars["spack"]["version"] == "from-b"


# --- cycle detection --------------------------------------------------------

def test_self_cycle():
    # A recipe that bases on itself. Built directly since parse() also checks
    # recipe_base targets exist (self-reference does exist).
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.r]
    recipe_base = "r"
    stack = ["debian"]
    """)
    with pytest.raises(ConfigError) as ei:
        resolve(layers, recipes, name="r")
    assert "cycle" in str(ei.value).lower()


def test_mutual_cycle():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    recipe_base = "b"
    [recipe.b]
    recipe_base = "a"
    """)
    with pytest.raises(ConfigError) as ei:
        resolve(layers, recipes, name="a")
    msg = str(ei.value).lower()
    assert "cycle" in msg


def test_diamond_is_not_a_cycle():
    # a -> b, a -> c, b -> d, c -> d : valid DAG, d's stack appears twice.
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.d]
    stack = ["debian"]
    [recipe.b]
    recipe_base = "d"
    stack = ["base"]
    [recipe.c]
    recipe_base = "d"
    stack = ["spack"]
    [recipe.a]
    recipe_base = ["b", "c"]
    stack = ["gcc"]
    """)
    rr = resolve(layers, recipes, name="a")
    assert rr.stack == ["debian", "base", "debian", "spack", "gcc"]


# --- anonymous recipes + --set ----------------------------------------------

def test_anonymous_stack():
    layers, recipes = cfg(LAYERS_TOML)
    rr = resolve(layers, recipes, stack=["debian", "spack"])
    assert rr.name == "<anonymous>"
    assert rr.stack == ["debian", "spack"]
    assert rr.layer_vars["spack"] == {"version": "v1.0.0"}


# --- image_tags -------------------------------------------------------------

def test_image_tags_resolved_from_named():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.r]
    stack = ["debian"]
    image_tags = ["latest", "ddm"]
    """)
    rr = resolve(layers, recipes, name="r")
    assert rr.image_tags == ["latest", "ddm"]


def test_image_tags_accumulate_base_first_deduped():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.a]
    stack = ["debian"]
    image_tags = ["latest", "shared"]
    [recipe.r]
    recipe_base = "a"
    stack = ["spack"]
    image_tags = ["shared", "own"]
    """)
    rr = resolve(layers, recipes, name="r")
    # base tags first, own appended, "shared" not duplicated
    assert rr.image_tags == ["latest", "shared", "own"]


def test_image_tags_empty_for_anonymous():
    layers, recipes = cfg(LAYERS_TOML)
    rr = resolve(layers, recipes, stack=["debian", "spack"])
    assert rr.image_tags == []


def test_set_highest_precedence_named():
    layers, recipes = cfg(LAYERS_TOML + """
    [recipe.r]
    stack = ["spack"]
    spack.version = "from-recipe"
    """)
    sets = parse_set_overrides(["spack.version=from-cli"])
    rr = resolve(layers, recipes, name="r", sets=sets)
    assert rr.layer_vars["spack"]["version"] == "from-cli"


def test_set_on_anonymous():
    layers, recipes = cfg(LAYERS_TOML)
    sets = parse_set_overrides(["debian.release=trixie", "spack.version=v9"])
    rr = resolve(layers, recipes, stack=["debian", "spack"], sets=sets)
    assert rr.layer_vars["debian"]["release"] == "trixie"
    assert rr.layer_vars["spack"]["version"] == "v9"


def test_parse_set_overrides_grouping():
    out = parse_set_overrides(["a.x=1", "a.y=2", "b.z=3"])
    assert out == {"a": {"x": "1", "y": "2"}, "b": {"z": "3"}}


def test_parse_set_value_with_equals():
    out = parse_set_overrides(["a.cmd=k=v"])
    assert out == {"a": {"cmd": "k=v"}}


def test_parse_set_bad_no_equals():
    with pytest.raises(ConfigError):
        parse_set_overrides(["a.x"])


def test_parse_set_bad_no_dot():
    with pytest.raises(ConfigError):
        parse_set_overrides(["x=1"])


# --- error cases ------------------------------------------------------------

def test_unknown_recipe():
    layers, recipes = cfg(LAYERS_TOML)
    with pytest.raises(ConfigError):
        resolve(layers, recipes, name="nope")


def test_anonymous_unknown_layer():
    layers, recipes = cfg(LAYERS_TOML)
    with pytest.raises(ConfigError) as ei:
        resolve(layers, recipes, stack=["debian", "ghost"])
    assert "ghost" in str(ei.value)


def test_set_unknown_layer_target():
    layers, recipes = cfg(LAYERS_TOML)
    sets = parse_set_overrides(["ghost.x=1"])
    with pytest.raises(ConfigError) as ei:
        resolve(layers, recipes, stack=["debian"], sets=sets)
    assert "ghost" in str(ei.value)


def test_requires_exactly_one_selector():
    layers, recipes = cfg(LAYERS_TOML)
    with pytest.raises(ValueError):
        resolve(layers, recipes)
    with pytest.raises(ValueError):
        resolve(layers, recipes, name="r", stack=["debian"])


def test_resolve_chain_guards_unknown_base():
    # parse() normally validates recipe_base targets, but resolve() guards too
    # for direct API use with hand-built recipes.
    layers = {"x": Layer(name="x", containerfile="FROM x\n")}
    recipes = {"r": Recipe(name="r", recipe_base=["ghost"], stack=["x"])}
    with pytest.raises(ConfigError) as ei:
        resolve(layers, recipes, name="r")
    assert "ghost" in str(ei.value)
