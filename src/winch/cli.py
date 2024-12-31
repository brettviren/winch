#!/usr/bin/env python

import click

from .util import setup_logging, debug, info
from .config import load as load_config
from .viz import write_dot
from .graph import Graph
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

