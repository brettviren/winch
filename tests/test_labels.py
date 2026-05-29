#!/usr/bin/env pytest
'''
Test winch2 image-label construction (see doc/winch2-plan.md section 12.8).
These tests need no podman.
'''

import tomllib

from winch.podman import label_args
from winch.graph import instance_labels, generate_instances
from winch.config import parse
from winch.recipe import resolve, _resolve_caps


# --- podman.label_args ------------------------------------------------------

def test_label_args_basic():
    args = label_args({"winch.layer": "spack", "winch.digest": "abc123"})
    # sorted by key: digest before layer
    assert args == [
        "--label", "winch.digest=abc123",
        "--label", "winch.layer=spack",
    ]


def test_label_args_stringifies_values():
    args = label_args({"winch.var.version": 14})
    assert args == ["--label", "winch.var.version=14"]


def test_label_args_value_with_equals_and_spaces():
    # No shell quoting needed (args go to podman as a list).
    args = label_args({"winch.var.cmd": "a=b c"})
    assert args == ["--label", "winch.var.cmd=a=b c"]


def test_label_args_empty():
    assert label_args({}) == []


# --- graph.instance_labels --------------------------------------------------

def test_instance_labels_fields():
    idata = {
        "kind": "spack",
        "version": "v1.1.0",
        "release": "trixie",
        "parent": {"image": "localhost/winch/debian:deadbeef0000"},
        "containerfile": "FROM ...\n",
        "image": "localhost/winch/spack:cafef00d1234",
    }
    labels = instance_labels("FULLDIGEST", idata, provides=["spack", "pkg:gcc@14"])
    assert labels["winch.layer"] == "spack"
    assert labels["winch.digest"] == "FULLDIGEST"
    assert labels["winch.var.version"] == "v1.1.0"
    assert labels["winch.var.release"] == "trixie"
    assert labels["winch.provides"] == "spack,pkg:gcc@14"
    # structural keys are not emitted as variables
    for k in labels:
        assert "winch.var.parent" != k
        assert "winch.var.containerfile" != k
        assert "winch.var.image" != k
        assert "winch.var.kind" != k


def test_instance_labels_no_provides():
    idata = {"kind": "base", "image": "x", "containerfile": "FROM x\n"}
    labels = instance_labels("D", idata)
    assert "winch.provides" not in labels
    assert labels == {"winch.layer": "base", "winch.digest": "D"}


def test_label_args_roundtrip_from_instance():
    args = label_args(instance_labels("D", {"kind": "k", "v": "1"}))
    assert args == [
        "--label", "winch.digest=D",
        "--label", "winch.layer=k",
        "--label", "winch.var.v=1",
    ]


# --- end-to-end: labels derived from a generated instance -------------------

CFG = """
[layer.debian]
release = "trixie"
provides = ["os:debian"]
containerfile = "FROM debian:{release}\\n"
[layer.spack]
version = "v1.1.0"
provides = ["spack", "pkg:gcc@{version}"]
requires = ["os:debian|os:alma"]
body = "RUN spack {version}\\n"
[recipe.r]
stack = ["debian", "spack"]
"""


def test_labels_for_generated_spack_layer():
    _, layers, recipes = parse(tomllib.loads(CFG))
    rr = resolve(layers, recipes, name="r")
    g = generate_instances(layers, [rr])

    spack_node = [n for n, d in g.I.nodes.data() if d["kind"] == "spack"][0]
    idata = g.data(spack_node)
    provides = _resolve_caps(layers["spack"].provides, rr.layer_vars["spack"])

    labels = instance_labels(spack_node, idata, provides=provides)
    assert labels["winch.layer"] == "spack"
    assert labels["winch.digest"] == spack_node
    assert labels["winch.var.version"] == "v1.1.0"
    assert labels["winch.provides"] == "spack,pkg:gcc@v1.1.0"
    # the digest label matches the actual node id (provenance is recoverable)
    args = label_args(labels)
    assert "--label" in args and f"winch.digest={spack_node}" in args
