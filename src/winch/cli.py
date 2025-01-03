#!/usr/bin/env python
'''
Command line interface to winch.
'''


import click

from .util import setup_logging, debug, warn, error, self_format
from .config import load as load_config
from .viz import write_dot
from .graph import Graph
from pathlib import Path

class Main:
    def __init__(self, config):
        self.opts = config.pop("winch",{})
        self.graph = Graph(**config)


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


@cli.command("list-nodes")
@click.option("-t","--nodetype", default='I',
              help='Type of nodes to list (I)instance (default) or (K)ind')
@click.pass_context
def list_nodes(ctx, nodetype):
    for node, ndat in ctx.obj.graph.nodes(nodetype):
        image = ndat.get("image","")
        label = ndat.get("label","")
        print(f'{nodetype} {node} {image} "{label}"')
        

@cli.command("render")
@click.option("-o","--outpath", default=None,
              help='A fully path name for output files, may include "{format}" markup')
@click.option("-t","--template", default='template',
              help='The parameter name to use for the content')
@click.option("-s","--string", default=None,
              help='A string to render, overrides template')
@click.option("-k","--kpath", multiple=True, default=[],
              help='Limit rendering to given K-path, default is all')
@click.pass_context
def render(ctx, outpath, template, string, kpath):
    '''
    Render the graph.
    '''
    if kpath:
        raise click.BadParameter('kpath limits not yet implemented')
    to_render = ctx.obj.graph.I.nodes

    for inode in to_render:
        idat = ctx.obj.graph.I.nodes[inode]
        if string:
            text = string
        else:
            try:
                text = idat[template]
            except KeyError:
                debug(f'no template in I-node {inode}: {idat}')
                continue
        text = text.format(**idat)
        if not outpath:
            print(text)
            continue
        path = Path(outpath.format(**idat))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        debug(path)

@cli.command("dot")
@click.option("-o","--output", default="/dev/stdout",
              help='Output for dot content')
@click.pass_context
def dot(ctx, output):
    '''
    Emit GraphViz dot representing the configured graph.
    '''
    write_dot(ctx.obj.graph.I, output)





def main() -> None:
    cli()

