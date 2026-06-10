#!/usr/bin/env python
'''
Recipe resolution for the winch2 "new paradigm".

A recipe (named [recipe.*] table or an anonymous --stack) is resolved into a
single ordered stack of layer names plus, for each layer, the variable map that
results from layering these precedence sources (lowest to highest):

  1. layer defaults from [layer.*]            (Layer.vars)
  2. recipe_base recipes, in order            (recursively resolved)
  3. this recipe's own layer-qualified vars
  4. CLI --set overrides

See doc/winch2-plan.md sections 12.3-12.5.  Consumed by the instance-chain
generator (winch-lnz) and capability validator (winch-ych).
'''

from dataclasses import dataclass, field

from .config import ConfigError, Recipe
from .util import self_format


@dataclass
class ResolvedRecipe:
    '''
    The fully resolved form of a recipe.

    - stack: ordered list of layer names (base first).  May contain a name more
      than once (e.g. diamond inheritance); the generator dedups by digest.
    - layer_vars: maps each distinct layer name in the stack to its resolved
      variable map (layer defaults overridden by the recipe/CLI chain).
    - image_tags: ordered list of tags to apply to the final built image,
      accumulated base-first through recipe_base.  The special value "latest"
      is left as-is here and expanded by the consumer.
    '''
    name: str
    stack: list = field(default_factory=list)
    layer_vars: dict = field(default_factory=dict)
    image_tags: list = field(default_factory=list)


def _merge_overrides(dst, src):
    '''
    Merge src override map into dst in place, last-wins per (layer, var).

    Both map a layer name to a dict of variable name to value.
    '''
    for layer_name, overrides in src.items():
        dst.setdefault(layer_name, {}).update(overrides)


def parse_set_overrides(items):
    '''
    Parse CLI --set "LAYER.VAR=VALUE" strings into an override map.

    Returns a dict mapping layer name to {var: value}.  Raises ConfigError on a
    malformed item.
    '''
    out = dict()
    for item in items or []:
        if "=" not in item:
            raise ConfigError(f'--set must be LAYER.VAR=VALUE, got "{item}"')
        key, value = item.split("=", 1)
        if "." not in key:
            raise ConfigError(f'--set key must be LAYER.VAR, got "{key}"')
        layer_name, var = key.split(".", 1)
        out.setdefault(layer_name, dict())[var] = value
    return out


def _resolve_chain(recipe, recipes, _path):
    '''
    Recursively resolve a recipe's effective stack and override map.

    Returns (stack, overrides, image_tags) where overrides holds only
    recipe-supplied layer-qualified variables (no layer defaults) and image_tags
    is the base-first accumulation of each recipe's image_tags (order-preserving,
    deduplicated).  Detects recipe_base cycles.
    '''
    if recipe.name in _path:
        cycle = " -> ".join(_path + [recipe.name])
        raise ConfigError(f'recipe_base cycle detected: {cycle}')
    path = _path + [recipe.name]

    stack = list()
    overrides = dict()
    image_tags = list()
    for base_name in recipe.recipe_base:
        base = recipes.get(base_name)
        if base is None:
            raise ConfigError(
                f'recipe "{recipe.name}" recipe_base names unknown '
                f'recipe "{base_name}"')
        base_stack, base_overrides, base_tags = _resolve_chain(base, recipes, path)
        stack += base_stack
        _merge_overrides(overrides, base_overrides)
        for tag in base_tags:
            if tag not in image_tags:
                image_tags.append(tag)

    stack += list(recipe.stack)
    _merge_overrides(overrides, recipe.layer_vars)
    for tag in recipe.image_tags:
        if tag not in image_tags:
            image_tags.append(tag)
    return stack, overrides, image_tags


def resolve(layers, recipes, name=None, stack=None, sets=None):
    '''
    Resolve a named or anonymous recipe into a ResolvedRecipe.

    - layers: dict of layer name to config.Layer.
    - recipes: dict of recipe name to config.Recipe.
    - name: a named recipe to resolve, OR
    - stack: a list of layer names for an anonymous recipe.
    - sets: an override map {layer: {var: value}} (e.g. from parse_set_overrides),
      applied at the highest precedence.

    Exactly one of name or stack must be given.  Raises ConfigError on unknown
    recipes/layers or recipe_base cycles.
    '''
    if (name is None) == (stack is None):
        raise ValueError("resolve() requires exactly one of name or stack")

    if name is not None:
        recipe = recipes.get(name)
        if recipe is None:
            raise ConfigError(f'no such recipe "{name}"')
        rname = name
        eff_stack, overrides, image_tags = _resolve_chain(recipe, recipes, [])
    else:
        rname = "<anonymous>"
        eff_stack = list(stack)
        overrides = dict()
        image_tags = list()

    if sets:
        _merge_overrides(overrides, sets)

    for layer_name in eff_stack:
        if layer_name not in layers:
            raise ConfigError(
                f'recipe "{rname}" stack names unknown layer "{layer_name}"')
    for layer_name in overrides:
        if layer_name not in layers:
            raise ConfigError(
                f'recipe "{rname}" sets variables on unknown layer "{layer_name}"')

    resolved = dict()
    for layer_name in eff_stack:
        if layer_name in resolved:
            continue
        layer = layers[layer_name]
        resolved[layer_name] = dict(layer.vars, **overrides.get(layer_name, {}))

    return ResolvedRecipe(name=rname, stack=eff_stack, layer_vars=resolved,
                          image_tags=image_tags)


def _resolve_caps(caps, variables):
    '''
    Self-format capability strings against a layer's (raw) resolved variables.

    Capabilities may reference layer variables, e.g. "pkg:gcc@{version}".  The
    variables are themselves resolved to a fixpoint alongside the capabilities
    so chained references (e.g. spec = "{package}@{version}") work too.
    '''
    if not caps:
        return []
    work = dict(variables)
    keys = list()
    for i, cap in enumerate(caps):
        key = f'__winch_cap_{i}__'
        work[key] = cap
        keys.append(key)
    self_format(work)
    return [work[key] for key in keys]


def formatted_provides(layer, variables):
    '''
    Return a layer's "provides" capabilities, self-formatted against its
    resolved variables (e.g. "pkg:gcc@{version}" -> "pkg:gcc@14").
    '''
    return _resolve_caps(layer.provides, variables)


def _requirement_satisfied(requirement, avail):
    '''
    A requirement is satisfied iff any of its "|"-separated alternatives is in
    avail (an entry with no "|" is a single exact alternative).
    '''
    return any(alt in avail for alt in requirement.split("|"))


def validate_capabilities(layers, resolved):
    '''
    Validate capability compatibility over a resolved recipe's stack.

    Walks the stack base->top accumulating each layer's (self-formatted)
    "provides" into a set.  Before adding a layer's own provides, every entry in
    its "requires" must be satisfied by what lies below it (OR within an entry
    via "|", AND across entries).

    Returns the full set of provided capabilities.  Raises ConfigError naming
    the recipe, layer, unmet requirement and the available set on failure.
    '''
    avail = set()
    for layer_name in resolved.stack:
        layer = layers[layer_name]
        variables = resolved.layer_vars[layer_name]

        for requirement in _resolve_caps(layer.requires, variables):
            if not _requirement_satisfied(requirement, avail):
                raise ConfigError(
                    f'recipe "{resolved.name}" layer "{layer_name}" requires '
                    f'"{requirement}" but the stack below provides '
                    f'{sorted(avail)}')

        avail.update(_resolve_caps(layer.provides, variables))

    return avail
