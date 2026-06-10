#!/usr/bin/env python

from pathlib import Path
from dataclasses import dataclass, field
import tomllib
import os


class ConfigError(Exception):
    '''
    Raised when a winch configuration is malformed.
    '''
    pass

def basedir(name = None, assure = True):
    '''
    Return a configuration directory.

    If name is given, it is included as a subdirectory.

    If assure then the directory will be created if not yet existing.

    '''
    path = Path(os.environ.get('XDG_CONFIG_DIR', os.environ['HOME'] + '/.config'))
    if name:
        path /= name
    if assure:
        path.mkdir(exist_ok=True, parents=True)
    return path


def load(path=None):
    '''
    Load and parse configuration at path.  
    '''
    if not path:
        path = basedir("winch") / "winch.toml"
    else:
        path = Path(path)
    if path.exists():
        return tomllib.load(path.open('rb'))
    raise FileNotFoundError(f'no configuration file found: {path}')


def merge(a, b):
    """
    Recursively merge to same-type things.

    Types must follow JSON data model.

    - lists are appended
    - scalars, b wins
    - objects are merged, b wins on key conflict.
    """
    if type(a) != type(b):
        raise ValueError(f'type mismatch {type(a)} != {type(b)}')

    if isinstance(a, list):
        return a+b

    if isinstance(a, (str, int, float)):
        return b;

    if isinstance(a, dict):
        a = dict(a)
        for key, value in b.items():
            if key in a:
                a[key] = merge(a[key], value);
                continue
            a[key] = value;
        return a

    raise TypeError(f'unsupported merge type: {type(a)}')


def load_many(*paths):
    '''
    Load one or more paths where each may be a comma-separated list of paths.
    '''

    my_paths=list()
    for path in paths:
        if "," in path:
            my_paths += path.split(",")
        else:
            my_paths.append(path)

    if not my_paths:
        my_paths = [None]       # will load single default

    cfg = load(my_paths[0])
    for path in my_paths[1:]:
        cfg = merge(cfg, load(path))
    return cfg


def build_search_path(cli_paths=(), env_path=None):
    '''
    Build the ordered directory list used to resolve relative config/include
    file names.

    - cli_paths: the -p/--path option values in flag order; each may be a
      ":"-separated list of directories.
    - env_path: the WINCH_PATH value (a ":"-separated list).  If None, it is
      read from the environment.

    The combined order is every -p directory (in flag order) followed by every
    WINCH_PATH directory.  This list is consulted only after the current working
    directory and the context directory during resolution.
    '''
    if env_path is None:
        env_path = os.environ.get("WINCH_PATH")
    chunks = list(cli_paths)
    if env_path:
        chunks.append(env_path)
    dirs = list()
    for chunk in chunks:
        for one in chunk.split(":"):
            if one:
                dirs.append(one)
    return dirs


def resolve_config_path(name, context=None, search=()):
    '''
    Resolve a config/include file name to an existing path (first-one-wins).

    An absolute name resolves to itself.  A relative name is searched, in order:

      1. the current working directory,
      2. the context directory (if given) -- the directory of the file whose
         "include" is being satisfied,
      3. each directory in search (the -p/--path then WINCH_PATH directories).

    Returns the first existing candidate as a Path.  Raises FileNotFoundError,
    listing what was tried, if none exist.
    '''
    p = Path(name)
    if p.is_absolute():
        if p.exists():
            return p
        raise FileNotFoundError(f'no such config file: {name}')

    candidates = [Path.cwd() / name]
    if context is not None:
        candidates.append(Path(context) / name)
    for d in search:
        candidates.append(Path(d) / name)

    for cand in candidates:
        if cand.exists():
            return cand

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f'no such config file "{name}"; tried: {tried}')


def _load_file(path, search, seen):
    '''
    Load one TOML file (at an already-resolved path), processing its
    "[winch] include" directive depth-first.

    - seen: set of absolute paths already loaded; guards against include cycles
      and diamond double-loading (an already-seen file contributes nothing).

    Includes are merged in listed order (later wins over earlier); the file's
    own content is merged last so it wins over anything it includes.  The
    "include" key is stripped so it never appears in the resulting config.
    '''
    abspath = path.resolve()
    if abspath in seen:
        return dict()
    seen.add(abspath)

    data = tomllib.load(abspath.open('rb'))

    includes = []
    win = data.get("winch")
    if isinstance(win, dict) and "include" in win:
        includes = _as_str_list(win.pop("include"),
                                f'[winch] include in {abspath}')

    result = dict()
    context = abspath.parent
    for inc in includes:
        incpath = resolve_config_path(inc, context=context, search=search)
        result = merge(result, _load_file(incpath, search, seen))
    return merge(result, data)


def load_config(config_files=(), search=()):
    '''
    Load and merge -c/--config files with include and search-path resolution.

    - config_files: the -c/--config values (each may be comma-separated).  These
      are resolved with NO context directory (relative names use CWD/search).
    - search: directories from build_search_path().

    Returns the merged configuration dict, or None when no config files are
    given and no default winch.toml exists (tolerated; commands report later).
    A missing explicitly-named file or include raises FileNotFoundError, and a
    malformed include directive raises ConfigError -- both fail fast.
    '''
    names = list()
    for cf in config_files:
        names += cf.split(",") if "," in cf else [cf]

    if not names:
        path = basedir("winch") / "winch.toml"
        if not path.exists():
            return None
        return _load_file(path, search, set())

    seen = set()
    cfg = dict()
    for name in names:
        path = resolve_config_path(name, context=None, search=search)
        cfg = merge(cfg, _load_file(path, search, seen))
    return cfg


#
# winch2 "new paradigm": stand-alone [layer.*] fragments composed by [recipe.*]
# tables.  See doc/winch2-plan.md section 12.  The functions below detect and
# parse a new-paradigm configuration.  Old-paradigm (bare kind tables with
# parent_kind) configurations are left untouched and handled by graph.Graph.
#

# Top-level keys that are not user "kind" tables in either paradigm.
RESERVED_TOPLEVEL = ("winch",)
# Top-level namespaces that mark the new paradigm.
NEW_NAMESPACES = ("layer", "recipe", "run")
# Layer keys with dedicated meaning (everything else is a layer variable).
LAYER_SPECIAL = ("provides", "requires", "body", "containerfile", "description")
# Recipe keys with dedicated meaning (everything else is layer-qualified vars).
RECIPE_SPECIAL = ("recipe_base", "stack", "description", "image_tags")
# Run keys with dedicated meaning (everything else is a run variable).
RUN_SPECIAL = ("image", "volumes", "podman_args", "command", "description")


@dataclass
class Layer:
    '''
    A stand-alone, parent-free container image layer fragment.

    - vars: scalar layer variables (the defaults a recipe may override).  May
      include an explicit "image" to override digest-based naming.
    - provides/requires: capability tag lists (see doc section 12.6).
    - body: Containerfile minus the FROM (winch injects "FROM {parent[image]}").
    - containerfile: full Containerfile, used verbatim (the escape hatch).
    - description: optional one-line human description of the layer.
    '''
    name: str
    vars: dict = field(default_factory=dict)
    provides: list = field(default_factory=list)
    requires: list = field(default_factory=list)
    body: str = None
    containerfile: str = None
    description: str = None


@dataclass
class Recipe:
    '''
    A composition of layers into one linear stack.

    - recipe_base: names of recipes to inherit from (in order).
    - stack: ordered list of layer names (base first).
    - layer_vars: maps a layer name to a dict of variable overrides.
    - description: optional one-line human description of the recipe.
    - image_tags: optional list of tags to apply to the final built image.
      The special value "latest" expands to "{recipe name}:latest"; any other
      value is used verbatim as the tag.
    '''
    name: str
    recipe_base: list = field(default_factory=list)
    stack: list = field(default_factory=list)
    layer_vars: dict = field(default_factory=dict)
    description: str = None
    image_tags: list = field(default_factory=list)


@dataclass
class Run:
    '''
    A description of how to run a built image with "winch run".

    - image: an image name, or "recipe.NAME" to use the leaf image of a recipe.
    - vars: free run variables (e.g. uid/gid), usable in f-string markup.  May
      reference recipe layer variables via "{layer.NAME.VAR}".
    - volumes: list of "-v" volume specs (host:ctr[:opts] or name:ctr[:opts]).
    - podman_args: list of extra "podman run" option strings (before the image).
    - command: default in-container command (a string, shell-split); overridden
      by any command given after "--" on the "winch run" command line.
    - description: optional one-line human description of the run.
    '''
    name: str
    image: str = None
    vars: dict = field(default_factory=dict)
    volumes: list = field(default_factory=list)
    podman_args: list = field(default_factory=list)
    command: str = None
    description: str = None


def _is_scalar(value):
    '''
    Return True if value is an acceptable scalar for a layer variable.
    '''
    return isinstance(value, (str, int, float, bool))


def _as_str_list(value, what):
    '''
    Normalize a string or list-of-string to a list-of-string.

    Raises ConfigError naming "what" on any other shape.
    '''
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        for elem in value:
            if not isinstance(elem, str):
                raise ConfigError(
                    f'{what} must be a string or list of strings, '
                    f'got element {elem!r}')
        return list(value)
    raise ConfigError(
        f'{what} must be a string or list of strings, got {type(value).__name__}')


def detect_paradigm(config):
    '''
    Return "new" or "old" for the given (merged) configuration dict.

    A config is "new" if it has any [layer.*] or [recipe.*] table.  It is an
    error (ConfigError) to mix new namespaces with old-style bare kind tables.
    '''
    has_new = any(ns in config for ns in NEW_NAMESPACES)
    if not has_new:
        return "old"

    # New paradigm: the only other allowed top-level keys are reserved (winch
    # options).  Any other top-level table is an old-style kind and signals an
    # illegal mix.
    strays = [k for k, v in config.items()
              if k not in NEW_NAMESPACES
              and k not in RESERVED_TOPLEVEL
              and isinstance(v, dict)]
    if strays:
        raise ConfigError(
            'configuration mixes new [layer.*]/[recipe.*] tables with '
            f'old-style kind tables: {sorted(strays)}.  A winch config must '
            'use either the old or the new paradigm, not both.')
    return "new"


def parse_layer(name, table):
    '''
    Parse one [layer.NAME] table into a Layer.
    '''
    if "parent_kind" in table:
        raise ConfigError(
            f'layer "{name}" has "parent_kind"; the new paradigm composes '
            'layers via [recipe.*]/--stack, not parent_kind.')

    provides = _as_str_list(table["provides"], f'layer "{name}" provides') \
        if "provides" in table else []
    requires = _as_str_list(table["requires"], f'layer "{name}" requires') \
        if "requires" in table else []

    body = table.get("body", None)
    if body is not None and not isinstance(body, str):
        raise ConfigError(f'layer "{name}" body must be a string')
    containerfile = table.get("containerfile", None)
    if containerfile is not None and not isinstance(containerfile, str):
        raise ConfigError(f'layer "{name}" containerfile must be a string')
    description = table.get("description", None)
    if description is not None and not isinstance(description, str):
        raise ConfigError(f'layer "{name}" description must be a string')

    variables = dict()
    for key, value in table.items():
        if key in LAYER_SPECIAL:
            continue
        if isinstance(value, list):
            raise ConfigError(
                f'layer "{name}" variable "{key}" is a list; the new paradigm '
                'has no list-valued variants (only provides/requires are lists).')
        if not _is_scalar(value):
            raise ConfigError(
                f'layer "{name}" variable "{key}" must be a scalar, '
                f'got {type(value).__name__}')
        variables[key] = value

    return Layer(name=name, vars=variables,
                 provides=provides, requires=requires,
                 body=body, containerfile=containerfile,
                 description=description)


def parse_recipe(name, table):
    '''
    Parse one [recipe.NAME] table into a Recipe.

    TOML nests dotted keys, so both "spack.version = ..." and a
    [recipe.NAME.spack] subtable arrive here as a dict value under "spack" and
    are treated uniformly as layer-qualified variables.
    '''
    recipe_base = _as_str_list(table["recipe_base"], f'recipe "{name}" recipe_base') \
        if "recipe_base" in table else []

    description = table.get("description", None)
    if description is not None and not isinstance(description, str):
        raise ConfigError(f'recipe "{name}" description must be a string')

    image_tags = _as_str_list(table["image_tags"], f'recipe "{name}" image_tags') \
        if "image_tags" in table else []

    stack = table.get("stack", [])
    if not isinstance(stack, list):
        raise ConfigError(f'recipe "{name}" stack must be a list of layer names')
    for elem in stack:
        if not isinstance(elem, str):
            raise ConfigError(
                f'recipe "{name}" stack must contain layer names (strings), '
                f'got {elem!r}')

    layer_vars = dict()
    for key, value in table.items():
        if key in RECIPE_SPECIAL:
            continue
        if not isinstance(value, dict):
            raise ConfigError(
                f'recipe "{name}" has unexpected key "{key}"={value!r}; layer '
                'variables must be written as "LAYER.VAR = value".')
        overrides = dict()
        for var, val in value.items():
            if not _is_scalar(val):
                raise ConfigError(
                    f'recipe "{name}" override "{key}.{var}" must be a scalar, '
                    f'got {type(val).__name__}')
            overrides[var] = val
        layer_vars[key] = overrides

    return Recipe(name=name, recipe_base=recipe_base,
                  stack=list(stack), layer_vars=layer_vars,
                  description=description, image_tags=image_tags)


def parse_run(name, table):
    '''
    Parse one [run.NAME] table into a Run.

    Keys in RUN_SPECIAL have dedicated meaning; every other (scalar) key is a
    run variable available to f-string markup (e.g. uid/gid).
    '''
    if "image" not in table:
        raise ConfigError(f'run "{name}" has no "image"')
    image = table["image"]
    if not isinstance(image, str):
        raise ConfigError(f'run "{name}" image must be a string')

    description = table.get("description", None)
    if description is not None and not isinstance(description, str):
        raise ConfigError(f'run "{name}" description must be a string')

    command = table.get("command", None)
    if command is not None and not isinstance(command, str):
        raise ConfigError(f'run "{name}" command must be a string')

    volumes = _as_str_list(table["volumes"], f'run "{name}" volumes') \
        if "volumes" in table else []
    podman_args = _as_str_list(table["podman_args"], f'run "{name}" podman_args') \
        if "podman_args" in table else []

    variables = dict()
    for key, value in table.items():
        if key in RUN_SPECIAL:
            continue
        if not _is_scalar(value):
            raise ConfigError(
                f'run "{name}" variable "{key}" must be a scalar, '
                f'got {type(value).__name__}')
        variables[key] = value

    return Run(name=name, image=image, vars=variables,
               volumes=volumes, podman_args=podman_args,
               command=command, description=description)


def parse_runs(config):
    '''
    Parse the [run.*] tables of a (merged) configuration dict.

    Returns a dict mapping run name to Run (empty if there are no run tables).
    Kept separate from parse() so existing (paradigm, layers, recipes) callers
    are unaffected.
    '''
    config = config or {}
    return {name: parse_run(name, table)
            for name, table in config.get("run", {}).items()}


def parse(config):
    '''
    Classify and parse a (merged) configuration dict.

    Returns a tuple (paradigm, layers, recipes):

    - paradigm: "old" or "new".
    - layers: dict mapping layer name to Layer (empty for old paradigm).
    - recipes: dict mapping recipe name to Recipe (empty for old paradigm).

    Raises ConfigError on a malformed or mixed configuration.
    '''
    config = config or {}
    paradigm = detect_paradigm(config)
    if paradigm == "old":
        return "old", {}, {}

    layers = {name: parse_layer(name, table)
              for name, table in config.get("layer", {}).items()}
    recipes = {name: parse_recipe(name, table)
               for name, table in config.get("recipe", {}).items()}

    # A recipe's stack and bases must name things that exist.
    for recipe in recipes.values():
        for base in recipe.recipe_base:
            if base not in recipes:
                raise ConfigError(
                    f'recipe "{recipe.name}" recipe_base names unknown '
                    f'recipe "{base}"')
        for layer_name in recipe.stack:
            if layer_name not in layers:
                raise ConfigError(
                    f'recipe "{recipe.name}" stack names unknown '
                    f'layer "{layer_name}"')
        for layer_name in recipe.layer_vars:
            if layer_name not in layers:
                raise ConfigError(
                    f'recipe "{recipe.name}" sets variables on unknown '
                    f'layer "{layer_name}"')

    return "new", layers, recipes
