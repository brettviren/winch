#!/usr/bin/env python

from pathlib import Path
import tomllib

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


