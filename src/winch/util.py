#!/usr/bin/env python
import os
import sys
import json
import shutil
import logging
import hashlib
from itertools import product
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("winch")
debug = log.debug
info = log.info
warn = log.warning
error = log.error


class TempDir:
    '''
    A temporary directory context manager.

    See tempfile.mkdtemp for suffix, prefix and dir.  

    If abandon is True (default is False) then the directory is not removed on exit.

    The directory path name is accessible from the "path" attribute.
    '''
    def __init__(self, suffix=None, prefix=None, dir=None, abandon=False):
        self.path = None
        self._args = (suffix, prefix, dir)
        self._abandon = abandon

    def __enter__(self):
        self.path = Path(tempfile.mkdtemp(*self._args))
        debug(f'made tmpdir: {self.path}')
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._abandon:
            debug(f'abandon tmpdir: {self.path}')
            return
        if self.path:
            import shutil
            debug(f'remove tmpdir: {self.path}')
            shutil.rmtree(self.path)


def setup_logging(log_output, log_level, log_format=None):

    try:
        level = int(log_level)      # try for number
    except ValueError:
        level = log_level.upper()   # else assume label
    log.setLevel(level)

    if log_format is None:
        log_format = '%(levelname)s %(message)s (%(filename)s:%(funcName)s)'
    log_formatter = logging.Formatter(log_format)

    def setup_handler(h):
        h.setLevel(level)
        h.setFormatter(log_formatter)
        log.addHandler(h)


    if not log_output:
        log_output = ["stderr"]
    for one in log_output:
        if one in ("stdout", "stderr"):
            setup_handler(logging.StreamHandler(getattr(sys, one)))
            continue
        setup_handler(logging.FileHandler(one))

    debug(f'logging to {log_output} at level {log_level}')

class SafeDict(dict):
    def __missing__(self, key):
        # print(f'MISSING: {key=}')
        return '{' + key + '}'

def self_format(dat: dict, return_changed=False, ignore_errors = False) -> dict:
    '''
    Format string values in dict dat using keys in same dict.

    The dat is changed in place.

    The formatted dat is and if return_changed is True the number of changes
    made is returned.
    '''

    # debug(json.dumps(dat))

    skip = set()
    nchanged = 0

    errors = set()

    while True:
        changed = False
        for k,v in dat.items():
            # descend into sub-dicts but only once
            if isinstance(v, dict):
                if k in skip:
                    continue
                dat[k], nch = self_format(v, True, ignore_errors)
                skip.add(k)
                if nch:
                    changed = True
                    nchanged += nch
                continue

            # Skip non-string values...
            if not isinstance(v, str):
                continue

            # Actually try to format
            try:
                newv = v.format_map(SafeDict(**dat))
            except TypeError as err:
                # warn(f'self_format type error with key "{k}":\n{err}')
                # raise

                ## This comes from, eg '{parent[release]}' when 'parent' is not
                ## defined eg in A-nodes.  It may be added later, eg for
                ## I-nodes.  Following "ignore what you don't know" philosophy
                ## we, err, ignore....
                continue
            except KeyError as err:
                errors.add(f'format error with key "{k}" and string "{v}", missing: {err}.  Check your config.')
                # This may come from referencing a missing dict key (not ours
                # directly, but one of our attributes which is of type dict).
                continue
            if newv == v:
                continue
            changed = True
            nchanged += 1
            dat[k] = newv
        if not changed:
            break
    if errors and not ignore_errors:
        for err in errors:
            warn(err)
        debug('resulting formatted dict:')
        debug('\n'.join([f'{k}:{v}' for k,v in dat.items()]))
    if return_changed:
        return dat, nchanged
    return dat

def digest(obj, hasher=hashlib.sha1):
    '''
    Return a hash digest of an object of various types.
    '''

    if isinstance(obj, str):
        return hasher(obj.encode('utf8')).hexdigest()

    if isinstance(obj, (list,tuple)):
        return digest(''.join([digest(one) for one in obj]))

    if isinstance(obj, int):
        return hasher(obj.to_bytes())

    if isinstance(obj, float):
        return digest(hash(obj))

    if isinstance(obj, dict):
        return digest([digest(i) for i in obj.items()])

    return digest(hash(obj))    # hail mary
        
    
def outer_product(dat, **common):
    '''
    Return list of dicts generated from any list-of-string attributes in
    dict dat.

    Each returned dict will choose one element of each list-of-string attributes
    to set that attribute.

    If no attributes are list-of-string, return [dat].

    Any attributes given in common will provide defaults.
    '''

    lkeys = list()
    lvals = list()

    for key,val in dat.items():
        if not val:
            # print(f'skipping empty {key=}')
            continue
        if isinstance(val, list):
            lkeys.append(key)
            lvals.append(val)
        else:
            common[key] = val

    if not lvals:
        return [common]

    ret = list()
    for lv in product(*lvals):
        ldat = {k:v for k,v in zip(lkeys, lv)}
        pars = dict(common, **ldat)
        ret.append(pars)
    return ret


def which(exe):
    '''
    Return a function that will run the given executable program.

    >>> podman = which("podman")
    >>> podman("images")
    >>> podman(["image","exists",image])

    The function takes a single positional argument of string or list of string.
    A string will invoke the executable in a shell.  Any keyword args are passed
    to subprocess.run().  The option "check=True" is set by default.
    '''
    path = shutil.which(exe)
    if path is None:
        raise FileNotFoundError(f'no such executable "{exe}"')
    def runner(args=None, **opts):
        opts.setdefault("check",True)
        if isinstance(args, str):
            opts['shell'] = True
            cmd = f'{path} {args}'
        else:
            cmd = [path]
            if args is not None:
                cmd += list(args)

        debug(f'running {cmd=} {opts=}')
        return subprocess.run(cmd, **opts)
    return runner


def assure_dir(path):
    '''
    Make directory at path if no such path exists (directory or file).
    '''
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)


def assure_file(path, content=None):
    '''
    Assure file exists at path with content (if given).

    Existing file with matching content is not changed.
    '''

    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    if content is not None and path.exists() and path.is_file():
        oldtext = path.read_text()
        if oldtext == content:
            return

    # fresh path and/or content
    path.write_text(content)
    

def looks_like_digest(thing):
    if not isinstance(thing, str):
        return False

    if len(thing) != 40:        # sha1
        return False

    try:
        int(thing, 16)
    except ValueError:
        return False

    return True

