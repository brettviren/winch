#!/usr/bin/env python

import click

from .util import setup_logging, debug, info
from .config import load as load_config
from .viz import write_dot
from .graph import Graph
from pathlib import Path

class Main:
    def __init__(self, config):
        self.opts = config.pop("winch",{})
        self.graph = Graph(config)


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
    ctx.obj = Main(load_config(config))
    return


@cli.command("list-inodes")
@click.option("-t","--types", default='KAI',
              help='Types of nodes to list')
@click.pass_context
def list_nodes(ctx, types):
    for node, ndat in ctx.obj.graph.graph.nodes.data():
        ntype = ndat.get("nodetype", None)
        if ntype in types:
            print(ntype,node)
        

@cli.command("render")
@click.option("-o","--outpath", default=None,
              help='A fully path name for output files, may include "{format}" markup')
@click.option("-t","--template", default='template',
              help='The parameter name to use for the content')
@click.option("--chain/--no-chain", default=True,
              help="Include inodes that are parents of the given indoes")
@click.argument("inodes", nargs=-1)
@click.pass_context
def render(ctx, outpath, template, chain, inodes):
    '''
    Render the given I-nodes in the graph.
    '''
    to_render = list()
    for inode in inodes:
        if chain:
            to_render += ctx.obj.graph.get_ichain(inode)
        else:
            to_render.append(inode)
    for inode in to_render:
        idat = ctx.obj.graph.graph.nodes[inode]
        text = idat[template]
        if not outpath:
            print(text)
            continue
        path = Path(outpath.format(**idat))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(path)

@cli.command("dot")
@click.option("-o","--output", default="/dev/stdout",
              help='Output for dot content')
@click.pass_context
def dot(ctx, output):
    '''
    Emit GraphViz dot representing the configured graph.
    '''
    write_dot(ctx.obj.graph.graph, output)





def main() -> None:
    cli()

