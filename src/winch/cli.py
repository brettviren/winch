#!/usr/bin/env python
'''
Command line interface to winch.
'''


import click

from .util import setup_logging, debug, warn, error, self_format, assure_file, SafeDict, looks_like_digest
from .config import load_many as load_configs, parse as parse_config, ConfigError
from .viz import write_dot
from .graph import Graph, generate_instances, instance_labels
from .recipe import (resolve as resolve_recipe, parse_set_overrides,
                     validate_capabilities, formatted_provides)
from .podman import build_image, image_exists, remove_image, image_copy, label_args
from pathlib import Path
import functools
import networkx as nx

# The implicit key to use when user does not provide key=value selector.  This
# key is domain specific so should not be hard-wired but instead a top-level CLI
# options should be used.  For now.... FIXME.
instance_attribute = 'image'

class Main:
    def __init__(self, config=None,):
        self.paradigm = None
        self.layers = {}
        self.recipes = {}
        if config is None:
            return
        self.opts = config.pop("winch",{})
        self.config = config
        try:
            self.paradigm, self.layers, self.recipes = parse_config(config)
        except ConfigError as err:
            raise click.ClickException(str(err))
        if self.paradigm == "old":
            self._graph = Graph(**config)

    @property
    def graph(self):
        if hasattr(self, '_graph'):
            return self._graph
        if self.paradigm == "new":
            raise click.ClickException(
                'this is a new-paradigm (layer/recipe) config; the graph-based '
                'commands are for old-paradigm configs.  Use "winch recipe".')
        raise click.BadParameter('no configuration provided.  Use "winch -c/--config" or set WINCH_CONFIG')

    def recipe_graph(self, name=None, stack=None, sets=None, validate=True):
        '''
        Resolve a new-paradigm recipe selector and generate its instance graph.

        - name: a named recipe, or
        - stack: a list of layer names (anonymous recipe), or
        - neither: the union of all named recipes (deduped by digest).
        - sets: an override map {layer: {var: value}} (highest precedence).
        - validate: check capability compatibility (default True); commands that
          only inspect (list/dot) may pass False to view incompatible stacks.

        Returns a Graph whose .I holds the resolved instance chain(s).
        '''
        if self.paradigm != "new":
            raise click.ClickException(
                '"winch recipe" requires a new-paradigm (layer/recipe) config.')
        try:
            if name is None and stack is None:
                resolved = [resolve_recipe(self.layers, self.recipes, name=rname)
                            for rname in self.recipes]
            else:
                resolved = [resolve_recipe(self.layers, self.recipes,
                                           name=name, stack=stack, sets=sets)]
            if validate:
                for rr in resolved:
                    validate_capabilities(self.layers, rr)
            return generate_instances(self.layers, resolved)
        except ConfigError as err:
            raise click.ClickException(str(err))


cmddef = dict(context_settings = dict(auto_envvar_prefix='WINCH',
                                      help_option_names=['-h', '--help']))
@click.option("-c", "--config", "config",
              multiple=True,
              help="Specify a config file")
@click.option("-l","--log-output", multiple=True,
              help="log to a file [default:stdout]")
@click.option("-L","--log-level", default="info",
              help="set logging level [default:info]")
@click.group("winch", **cmddef)
@click.pass_context
def cli(ctx, config, log_output, log_level):
    '''
    winch - Wire-Cell Toolkit image node container harness
    '''
    setup_logging(log_output, log_level)
    try:
        cfg = load_configs(*config)
    except FileNotFoundError:
        cfg = None

    ctx.obj = Main(cfg)
    return


@cli.command("kpaths")
@click.pass_context
def cmd_kpaths(ctx):
    for one in ctx.obj.graph.kpaths():
        print(','.join(one))
            

def to_nodes(gr, desc):
    '''
    Return node name given description.

    Description is string or list-of-string.  list-of-string returned for both.
    '''
    if isinstance(desc, str):
        return to_nodes(gr, [desc])

    ret = list()
    for one in desc:
        if looks_like_digest(one):
            ret.append(one)
            continue
        if '=' in one:
            key, value = one.split("=", 1)
        else:
            key = instance_attribute
            value = one
        ret += [n for n,d in gr.nodes.data() if d.get(key, None) == value]
    return ret

def select_inodes(ctx, kpath=None, kind=None, deps=None, instances=None, none_is_all=False):
    '''
    Select instance nodes from the I-graph returning their node IDs.
    '''

    if kpath:
        kpath = tuple(kpath.split(","))
        ret = list()
        for knode, inodes in zip(kpath, ctx.obj.graph.from_kpath(kpath)):
            ret += inodes
        return inodes

    if deps:
        ret = list()
        for inode in to_nodes(ctx.obj.graph.I, deps):
            for got in ctx.obj.graph.ipath(inode):
                ret.append(got)
        return ret

    if kind:
        return ctx.obj.graph.from_kind(kind)

    if not instances and none_is_all:
        instances = 'all'

    if not instances:
        return []
    
    if instances == "all":
        return ctx.obj.graph.I.nodes

    print(f'{instances=}')
    return to_nodes(ctx.obj.graph.I, instances.split(","))


def selection(none_is_all=False):
    def decorator(func):
        '''
        A decorator for a command applied to a selection of I-nodes.

        It provides a single 'inodes' attribute
        '''
        @click.option("-k","--kind", default=None, type=str,
                      help='Limit to I-nodes made from K-node regardless of path')
        @click.option("-d","--deps", default=None, type=str,
                      help='Limit to I-nodes on which the given inode depends.')
        @click.option("-i","--instances", default=None, type=str,
                      help='Limit to specific I-nodes.')
        @click.pass_context
        @functools.wraps(func)
        def wrapper(ctx, *args, **kwds):
            kpath = kwds.pop('kpath',None)
            kind = kwds.pop('kind',None)
            deps = kwds.pop('deps',None)
            instances = kwds.pop('instances',None)
            inodes = select_inodes(ctx, kpath, kind, deps, instances, none_is_all)
            if not inodes:
                warn(f'no instances found')
            kwds['inodes'] = inodes
            return func(*args, **kwds)
        return wrapper
    return decorator


def old_select_options(func):
    '''
    Add the old-paradigm I-node selection options (-k/-d/-i) to a command.
    '''
    func = click.option("-i","--instances", default=None, type=str,
                        help='Limit to specific I-nodes (old paradigm).')(func)
    func = click.option("-d","--deps", default=None, type=str,
                        help='Limit to I-nodes on which the given inode depends (old paradigm).')(func)
    func = click.option("-k","--kind", default=None, type=str,
                        help='Limit to I-nodes made from K-node regardless of path (old paradigm).')(func)
    return func


def recipe_select_options(func):
    '''
    Add the new-paradigm recipe-selector options (NAME/--stack/--set) to a command.
    '''
    func = click.argument("name", required=False)(func)
    func = click.option("--set", "sets", multiple=True, metavar="LAYER.VAR=VALUE",
                        help='Override a layer variable (repeatable, new paradigm).')(func)
    func = click.option("--stack", default=None,
                        help='Anonymous recipe: comma-separated layer names (new paradigm).')(func)
    return func


def graph_and_inodes(ctx, kind=None, deps=None, instances=None,
                     name=None, stack=None, sets=(), none_is_all=False):
    '''
    Resolve the (graph, inodes) pair for an inspection command in either
    paradigm.

    - New paradigm: build the graph from a recipe selector (NAME or
      --stack/--set; no selector -> union of all named recipes) and return its
      nodes in dependency order.  Capabilities are NOT validated here so
      incompatible stacks can still be inspected.
    - Old paradigm: use the configured graph and the -k/-d/-i selection.
    '''
    main = ctx.obj
    if main.paradigm == "new":
        stack_list = stack.split(",") if stack else None
        try:
            overrides = parse_set_overrides(sets)
        except ConfigError as err:
            raise click.ClickException(str(err))
        graph = main.recipe_graph(name=name, stack=stack_list, sets=overrides,
                                  validate=False)
        return graph, list(nx.topological_sort(graph.I))

    inodes = select_inodes(ctx, None, kind, deps, instances, none_is_all)
    return main.graph, inodes


@cli.command("dump-config")
@click.pass_context
def cmd_config(ctx):
    import json
    print(json.dumps(ctx.obj.config))

@cli.command("list")
@recipe_select_options
@old_select_options
@click.option("-t","--template", default="{image}",
              help="The template for display")
@click.pass_context
def cmd_list(ctx, name, stack, sets, kind, deps, instances, template):
    '''
    List things about the winch graph.

    Old paradigm: with -k/-d/-i, list the matching I-nodes (default: all).

    New paradigm: with no selector, list the defined layers and recipes; with a
    recipe NAME or --stack/--set, list that recipe's resolved instance chain.
    '''
    main = ctx.obj
    template = template.replace('\\n','\n').replace('\\t','\t')

    if main.paradigm == "new" and not name and not stack:
        for lname in sorted(main.layers):
            print(f'layer {lname}')
        for rname in sorted(main.recipes):
            print(f'recipe {rname}')
        return

    graph, inodes = graph_and_inodes(ctx, kind, deps, instances,
                                     name, stack, sets, none_is_all=True)
    for inode in inodes:
        data = graph.data(inode)
        string = template.format_map(SafeDict(ntype='I', node=inode, **data))
        print(string)

    

@cli.command("build", context_settings=dict(
    ignore_unknown_options=True,
    allow_extra_args=True # This is also useful for accepting arguments after known options
))
@selection()
@click.option("--containerfile-attribute", default="containerfile",
              help="Name the attribute providing the Containerfile content")
@click.option("--image-attribute", default="image",
              help="Name the attribute providing the image name")
@click.option("-r","--rebuild", default="all",
              type=click.Choice(["none","all","deps","last"]),
              help="Control what to let podman attempt to rebuild if image exists")              
@click.option("-f","--force", default="none",
              type=click.Choice(["none","all","deps","last"]),
              help="Force a rebuild by removing existing image that maps the selector")              
@click.option("-o","--outpath", default='winch-contexts/{image}/Containerfile',
              help='A file path name for output files, may include "{format}" markup')
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def build(ctx, inodes, containerfile_attribute, image_attribute, rebuild, force, outpath, args):
    '''
    Build container images from I-nodes.

    This will also build Containerfile file and context directory in output.

    The --rebuild option allows podman to determine if a layer needs rebuilding
    (eg due to a config.toml change) and does not necessarily lead to a rebuild.

    The --force option will remove an image and thus cause a rebuild regardless
    if one is needed or not.
    '''
    _build_inodes(ctx.obj.graph, inodes, containerfile_attribute, image_attribute,
                  rebuild, force, outpath, args)


def _build_inodes(graph, inodes, containerfile_attribute, image_attribute,
                  rebuild, force, outpath, args, labels_for=None):
    '''
    Build the given ordered I-nodes from a graph.

    Shared by the old-paradigm "build" and the new-paradigm "recipe" commands.

    - inodes: I-node ids in dependency order (parents before children).
    - labels_for: optional callable(inode, idata) -> list of extra "podman
      build" arguments (e.g. winch.* --label args); used by "recipe".
    '''
    for inode in inodes:
        idata = graph.data(inode)
        image = idata[image_attribute]

        exists = image_exists(image)
        debug(f'{exists=} {inode=} {image=} {force=} {rebuild=}')

        extra_args = list()
        if (force == "all"
            or
            (force == "deps" and inode != inodes[-1])
            or
            (force == "last" and inode == inodes[-1])):
            print(f'force-removing existing image: {image}')
            remove_image(image)
            extra_args.append("--no-cache")
            debug(f'building {image} with no cache')

        if exists and (
                rebuild == "none"
                or
                (rebuild == "deps" and inode == inodes[-1])
                or
                (rebuild == "last" and inode != inodes[-1])):
            print(f'not rebuilding existing image: {image}')
            continue
        try:
            cfile = idata[containerfile_attribute]
        except KeyError:
            debug(f'{inode} "{image}" lacks {containerfile_attribute}, skipping')
            continue
        cpath = outpath.format(node=inode, **idata)
        assure_file(cpath, cfile)

        for fpath, fcont in idata.pop('files', {}).items():
            debug(f'{fpath=}\n{fcont}\n')
            fpath = Path(cpath).parent / fpath.format(node=inode, **idata)
            fcont = fcont.format_map(SafeDict(node=inode, **idata))
            assure_file(fpath, fcont)

        debug(f'{idata=}')
        image_format = idata.get("image_format", None)
        if image_format:
            debug(f'using image format "{image_format}"')
            extra_args.append(f'--format={image_format}')

        if labels_for is not None:
            extra_args += labels_for(inode, idata)

        extra_args += args
        build_image(image, cpath, *extra_args)


@cli.command("recipe", context_settings=dict(
    ignore_unknown_options=True,
    allow_extra_args=True))
@click.option("--stack", default=None,
              help="Anonymous recipe: a comma-separated list of layer names")
@click.option("--set", "sets", multiple=True, metavar="LAYER.VAR=VALUE",
              help="Override a layer variable (repeatable, highest precedence)")
@click.option("--containerfile-attribute", default="containerfile",
              help="Name the attribute providing the Containerfile content")
@click.option("--image-attribute", default="image",
              help="Name the attribute providing the image name")
@click.option("-r","--rebuild", default="all",
              type=click.Choice(["none","all","deps","last"]),
              help="Control what to let podman attempt to rebuild if image exists")
@click.option("-f","--force", default="none",
              type=click.Choice(["none","all","deps","last"]),
              help="Force a rebuild by removing existing image that maps the selector")
@click.option("-o","--outpath", default='winch-contexts/{kind}-{node}/Containerfile',
              help='A file path name for output files, may include "{format}" markup')
@click.argument("name", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def recipe(ctx, stack, sets, containerfile_attribute, image_attribute,
           rebuild, force, outpath, name, args):
    '''
    Build container images from a new-paradigm recipe.

    Give either a named recipe NAME or an anonymous recipe via --stack.  Layer
    variables may be overridden with repeated --set LAYER.VAR=VALUE (highest
    precedence).

    Capabilities are validated before any image is built.  Each built image
    carries winch.* provenance labels (layer, digest, variables, provides).

    Examples:

      winch recipe phlex-debian
      winch recipe --stack debian,spack --set debian.release=trixie
    '''
    main = ctx.obj
    if main.paradigm != "new":
        raise click.ClickException(
            '"winch recipe" requires a new-paradigm (layer/recipe) config.')

    if stack and name:
        raise click.ClickException(
            'give either a recipe NAME or --stack, not both '
            f'(got name="{name}" and --stack="{stack}")')
    if not stack and not name:
        raise click.ClickException('give a recipe NAME or --stack <layers>')

    stack_list = stack.split(",") if stack else None
    try:
        overrides = parse_set_overrides(sets)
        rr = resolve_recipe(main.layers, main.recipes,
                            name=name, stack=stack_list, sets=overrides)
        # Validate capabilities before any podman call.
        validate_capabilities(main.layers, rr)
        graph = generate_instances(main.layers, [rr])
    except ConfigError as err:
        raise click.ClickException(str(err))

    leaves = [n for n in graph.I.nodes() if graph.I.out_degree(n) == 0]
    if not leaves:
        warn(f'recipe "{rr.name}" has no layers to build')
        return
    inodes = graph.ipath(leaves[0])

    def labels_for(inode, idata):
        layer = main.layers[idata["kind"]]
        provides = formatted_provides(layer, rr.layer_vars[idata["kind"]])
        return label_args(instance_labels(inode, idata, provides))

    _build_inodes(graph, inodes, containerfile_attribute, image_attribute,
                  rebuild, force, outpath, args, labels_for=labels_for)


@cli.command("render")
@recipe_select_options
@old_select_options
@click.option("-T", "--template-attribute", default=None,
              help="Name the attribute providing the content to render")
@click.option("-t", "--template", default=None,
              help="The template text to render")
@click.option("-o","--outpath", default=None,
              help='A file path name for output files, may include "{format}" markup')
@click.pass_context
def render(ctx, name, stack, sets, kind, deps, instances,
           template, template_attribute, outpath):
    '''
    Render a template to a file.

    Either -T/--template-attribute or -t/--template are requird

    Old paradigm: select I-nodes with -k/-d/-i.  New paradigm: select with a
    recipe NAME or --stack/--set (no selector renders all named recipes).

    If not -o/--outpath is given, output is to stdout.
    '''
    if not any((template, template_attribute)):
        raise click.BadParameter('must provide template or template attribute')

    if outpath is None:
        outpath = '/dev/stdout'

    graph, inodes = graph_and_inodes(ctx, kind, deps, instances, name, stack, sets)
    for inode in inodes:
        idata = graph.data(inode)
        opath = outpath.format_map(SafeDict(node=inode, **idata))
        if template_attribute is not None:
            try:
                tmpl = idata[template_attribute]
            except KeyError:
                warn(f'no template attribute {template_attribute} in node {inode}, skipping')
                continue
        else:
            tmpl = template
        tmpl = tmpl.replace('\\n','\n').replace('\\t','\t')
        otext = tmpl.format_map(SafeDict(node=inode, **idata))
        assure_file(opath, otext)


@cli.command("extract")
@click.option("-i","--image", default=None, type=str,
              help='Name the podman image.')
@click.option("-o","--output", default=".", type=str,
              help='Path of file or directory to save extracted file.')
@click.argument("path")
def extract(image, output, path):
    '''
    Extract (cp) a path from an image to the host output path.
    '''
    image_copy(image, path, output)



@cli.command("dot")
@recipe_select_options
@click.option("-o","--output", default="/dev/stdout",
              help='Output for dot content')
@click.option("-t","--template", default="{image}\n{node}",
              help="The template node label")
@click.pass_context
def dot(ctx, name, stack, sets, output, template):
    '''
    Emit GraphViz dot representing the configured graph.

    Old paradigm: the whole configured I-graph.  New paradigm: a recipe NAME or
    --stack/--set selects the chain(s); with no selector the union of all named
    recipes is shown.
    '''
    main = ctx.obj
    if main.paradigm == "new":
        stack_list = stack.split(",") if stack else None
        try:
            overrides = parse_set_overrides(sets)
        except ConfigError as err:
            raise click.ClickException(str(err))
        graph = main.recipe_graph(name=name, stack=stack_list, sets=overrides,
                                  validate=False)
    else:
        graph = main.graph

    I = graph.I
    for node, data in I.nodes.data():
        label = template.format_map(SafeDict(ntype='I', node=node, **data))
        I.nodes[node].clear()
        I.nodes[node]["label"] = label

    write_dot(I, output)





def main() -> None:
    cli()

