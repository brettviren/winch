#!/usr/bin/env python
import os
import sys
import logging
import hashlib
from itertools import product

log = logging.getLogger("winch")
debug = log.debug
info = log.info
warn = log.warning
error = log.error

def setup_logging(log_output, log_level):
    try:
        level = int(log_level)      # try for number
    except ValueError:
        level = log_level.upper()   # else assume label
    log.setLevel(level)

    if not log_output:
        log_output = ["stderr"]
    for one in log_output:
        if one in ("stdout", "stderr"):
            sh = logging.StreamHandler(getattr(sys, one))
            sh.setLevel(level)
            log.addHandler(sh)
            continue
        fh = logging.FileHandler(one)
        fh.setLevel(level)
        log.addHandler(fh)

    debug(f'logging to {log_output} at level {log_level}')

class SafeDict(dict):
    def __missing__(self, key):
        # print(f'MISSING: {key=}')
        return '{' + key + '}'

def self_format(dat: dict, return_changed=False) -> dict:
    '''
    Format string values in dict dat using keys in same dict.

    The dat is changed in place.

    The formatted dat is and if return_changed is True the number of changes
    made is returned.
    '''

    skip = set()
    nchanged = 0
    while True:
        changed = False
        for k,v in dat.items():

            # descend into sub-dicts but only once
            if isinstance(v, dict):
                if k in skip:
                    continue
                dat[k], nch = self_format(v, True)
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
            except TypeError:
                # error(f'error in formatting: {k=} -> {v=}')
                # raise

                ## This comes from, eg '{parent[release]}' when 'parent' is not
                ## defined eg in A-nodes.  It may be added later, eg for
                ## I-nodes.  Following "ignore what you don't know" philosophy
                ## we, err, ignore....
                continue
            if newv == v:
                continue
            changed = True
            nchanged += 1
            dat[k] = newv
        if not changed:
            break
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

