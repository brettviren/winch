#!/usr/bin/env python
'''
Run resolution for the winch "new paradigm".

A [run.NAME] table (config.Run) describes how to launch a built image with
"winch run".  Resolving a run means:

  1. Determine the image.  If run.image is "recipe.NAME", the recipe is resolved
     and built-graph leaf image name is used, and that recipe's resolved layer
     variables form the namespace for {layer.NAME.VAR} references.  Otherwise the
     image string is used verbatim and no layer references are available.
  2. Expand {layer.NAME.VAR} references (against the recipe's resolved layers).
  3. Self-format the run's free variables (e.g. uid/gid) to a fixpoint.
  4. Format the podman_args, volumes and command against those variables.

The resolved run carries the literal pieces "winch run" assembles into a
"podman run" command line.
'''

from dataclasses import dataclass, field

from .config import ConfigError
from .util import self_format, SafeDict, find_unresolved
from .recipe import resolve as resolve_recipe
from .graph import generate_instances, _LAYER_REF_RE

RECIPE_IMAGE_PREFIX = "recipe."


@dataclass
class ResolvedRun:
    '''
    The fully resolved, ready-to-launch form of a run.

    - image: the concrete image name to run.
    - podman_args: extra "podman run" option strings (placed before the image).
    - volumes: "-v" volume specs.
    - command: default in-container command string, or None (image default).
    '''
    name: str
    image: str = None
    podman_args: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    command: str = None


def _layer_substituter(layer_ns, run_name):
    '''
    Return a re.sub replacement function for {layer.NAME.VAR} markup that looks
    values up in layer_ns (a {layer_name: {var: value}} map) and raises a
    ConfigError naming run_name on any unknown reference.
    '''
    def sub(m):
        lname, vname = m.group(1), m.group(2)
        if lname not in layer_ns:
            raise ConfigError(
                f'run "{run_name}": {{layer.{lname}.{vname}}} references layer '
                f'"{lname}" which is not in the run\'s recipe (the run\'s image '
                'must be "recipe.NAME" to use {layer.*} references)')
        if vname not in layer_ns[lname]:
            raise ConfigError(
                f'run "{run_name}": {{layer.{lname}.{vname}}} - layer "{lname}" '
                f'has no variable "{vname}"')
        return str(layer_ns[lname][vname])
    return sub


def resolve_run(run, layers, recipes, sets=None):
    '''
    Resolve a config.Run into a ResolvedRun.

    - run: the config.Run to resolve.
    - layers, recipes: the parsed new-paradigm config maps.
    - sets: an override map {layer: {var: value}} applied when resolving a
      "recipe.NAME" image, so the run sees the same layer values the image was
      (or would be) built with.

    Raises ConfigError on unknown recipes/layers, unresolvable {layer.*}
    references, or leftover unresolved markup.
    '''
    layer_ns = dict()
    if run.image.startswith(RECIPE_IMAGE_PREFIX):
        rname = run.image[len(RECIPE_IMAGE_PREFIX):]
        rr = resolve_recipe(layers, recipes, name=rname, sets=sets)
        graph = generate_instances(layers, [rr])
        leaves = [n for n in graph.I.nodes() if graph.I.out_degree(n) == 0]
        if not leaves:
            raise ConfigError(
                f'run "{run.name}" recipe "{rname}" builds no image')
        image = graph.data(leaves[0])["image"]
        layer_ns = rr.layer_vars
    else:
        image = run.image

    sub = _layer_substituter(layer_ns, run.name)

    def expand(value):
        '''Expand {layer.*} markup in a string (pass non-strings through).'''
        if not isinstance(value, str):
            return value
        return _LAYER_REF_RE.sub(sub, value)

    # Resolve the free run variables: expand {layer.*}, then self-format so a
    # variable may reference another (e.g. spec = "{package}@{version}").
    varns = {k: expand(v) for k, v in run.vars.items()}
    self_format(varns)

    def render_list(items):
        out = list()
        for item in items:
            out.append(expand(item).format_map(SafeDict(**varns)))
        return out

    podman_args = render_list(run.podman_args)
    volumes = render_list(run.volumes)
    command = None
    if run.command is not None:
        command = expand(run.command).format_map(SafeDict(**varns))

    # Catch leftover unresolved {var} markup (SafeDict left them intact).
    originals = dict()
    for i, item in enumerate(run.podman_args):
        originals[f'podman_args[{i}]'] = expand(item)
    for i, item in enumerate(run.volumes):
        originals[f'volumes[{i}]'] = expand(item)
    if run.command is not None:
        originals['command'] = expand(run.command)
    unresolved = find_unresolved(originals, varns)
    if unresolved:
        detail = "; ".join(f'{k}: {refs}' for k, refs in sorted(unresolved.items()))
        raise ConfigError(
            f'run "{run.name}" has unresolved markup: {detail}')

    return ResolvedRun(name=run.name, image=image,
                       podman_args=podman_args, volumes=volumes,
                       command=command)


def build_run_command(resolved, extra_opts=(), command_override=None):
    '''
    Assemble the token list that follows "podman run" for a ResolvedRun.

    - extra_opts: CLI passthrough podman options, placed after the run's own
      podman_args but before the image (so the CLI can override the config).
    - command_override: command tokens to run instead of the configured command
      (e.g. tokens given after "--"); None/empty falls back to run.command, and
      that failing, the image's own default.

    Order: podman_args, extra_opts, "-v" volume pairs, image, command.
    '''
    import shlex
    argv = list(resolved.podman_args) + list(extra_opts)
    for vol in resolved.volumes:
        argv += ["-v", vol]
    argv.append(resolved.image)
    if command_override:
        argv += list(command_override)
    elif resolved.command:
        argv += shlex.split(resolved.command)
    return argv


def split_passthrough(args):
    '''
    Split "winch run" passthrough tokens into (podman_opts, command_override) at
    the first standalone "--".  With no "--", all tokens are podman options and
    command_override is None.
    '''
    args = list(args)
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i + 1:]
    return args, None
