#!/usr/bin/env pytest
'''
Test winch.util
'''

from winch.util import self_format, TempDir

def test_self_format():
    p = dict(kind="debian", release="bookworm")
    d = dict(kind="debian_minimal",
             instance="{kind}-{parent[release]}",
             parent_kind="debian",
             parent=p)

    f = self_format(d)
    for k,v in f.items():
        if isinstance(v,dict):
            continue
        assert '{' not in v
        assert '}' not in v
    assert f['instance'] == 'debian_minimal-bookworm'

def test_tempdir():
    with TempDir() as tmp:
        assert tmp.exists()
        assert tmp.is_dir()

    assert not tmp.exists()
    
