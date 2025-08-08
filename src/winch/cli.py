#!/usr/bin/env python
'''
Command line interface to winch.
'''


import click

from .util import setup_logging, debug, warn, error, self_format, assure_file, SafeDict, looks_like_digest
from .config import load as load_config
from .viz import write_dot
from .graph import Graph
from .podman import build_image, image_exists, remove_image, image_copy
from pathlib import Path
import functools

# The implicit key to use when user does not provide key=value selector.  This
# key is domain specific so should not be hard-wired but instead a top-level CLI
# options should be used.  For now.... FIXME.
instance_attribute = 'image'

class Main:
    def __init__(self, config=None,):
        if config is None:
            return
        self.opts = config.pop("winch",{})
        self._graph = Graph(**config)

    @property
    def graph(self):
        if hasattr(self, '_graph'):
            return self._graph
        raise click.BadParameter('no configuration provided.  Use "winch -c/--config" or set WINCH_CONFIG')


cmddef = dict(context_settings = dict(auto_envvar_prefix='WINCH',
                                      help_option_names=['-h', '--help']))
@click.option("-c", "--config", default=None, type=str,
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
        cfg = load_config(config)
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

    return to_nodes(instances.split(","))


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


@cli.command("list")
@selection(none_is_all=True)
@click.option("-t","--template", default="{image}",
              help="The template for display")
@click.pass_context
def cmd_list(ctx, inodes, template):
    '''
    List things about the winch graph.

    Default will list all K-nodes.

    Providing -K/--kpath lists I-nodes from the K-graph path.  A special K-graph
    path of "all" will list all K-graph paths and no I-nodes.

    Providing -k/--kind lists I-nodes produced from the K-node regardless of
    K-graph path.

    Providing -d/--deps gives an I-node as its node name (hash) or an
    "key=value" attribute.

    Providing -i/--instances gives a comma-separated list of I-node, each specified
    by a node name (hash) or an "key=value" attribute.  A special entry "all"
    will match all I-nodes.
    '''
    template = template.replace('\\n','\n').replace('\\t','\t')
    for inode in inodes:
        data = ctx.obj.graph.data(inode)
        string = template.format_map(SafeDict(ntype='I', node=inode, **data))
        print(string)



@cli.command("build")
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
@click.pass_context
def build(ctx, inodes, containerfile_attribute, image_attribute, rebuild, force, outpath):
    '''
    Build container images from I-nodes.

    This will also build Containerfile file and context directory in output.
    '''
    for inode in inodes:
        idata = ctx.obj.graph.data(inode)
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

        build_image(image, cpath, *extra_args)


@cli.command("render")
@selection()
@click.option("-T", "--template-attribute", default=None,
              help="Name the attribute providing the content to render")
@click.option("-t", "--template", default=None,
              help="The template text to render")
@click.option("-o","--outpath", default=None,
              help='A file path name for output files, may include "{format}" markup')
@click.pass_context
def render(ctx, inodes, template, template_attribute, outpath):
    '''
    Render a template to a file.

    Either -T/--template-attribute or -t/--template are requird

    If not -o/--outpath is given, output is to stdout.
    '''
    if not any((template, template_attribute)):
        raise click.BadParameter('must provide template or template attribute')

    if outpath is None:
        outpath = '/dev/stdout'

    for inode in inodes:
        idata = ctx.obj.graph.data(inode)
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
@click.option("-o","--output", default="/dev/stdout",
              help='Output for dot content')
@click.option("-t","--template", default="{image}\n{node}",
              help="The template node label")
@click.pass_context
def dot(ctx, output, template):
    '''
    Emit GraphViz dot representing the configured graph.
    '''
    I = ctx.obj.graph.I
    for node, data in I.nodes.data():
        label = template.format(ntype='I', node=node, **data)
        I.nodes[node].clear()
        I.nodes[node]["label"] = label

    write_dot(I, output)





def main() -> None:
    cli()

