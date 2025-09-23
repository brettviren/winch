#!/usr/bin/env python

from pathlib import Path
import tomllib
import os

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

    cfg = load(my_paths.pop(0))
    for path in my_paths:
        new = load(my_paths.pop(0))
        cfg = merge(cfg, new)
    return cfg
