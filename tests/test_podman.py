#!/usr/bin/env pytest

from winch.util import TempDir
from winch import podman

def test_exists():
    assert not podman.image_exists("winch-test-never-make-this")

def test_pull():
    imghash = podman.pull_image("debian:bookworm")
    if imghash != '11c49840db5438765202fd3f2251fcacdf4776faaa3fc018a462bf354963623f':
        print(f'debian:bookworm changed: "{imghash}"')
    assert podman.image_exists("debian:bookworm")


def test_build():
    name = 'winch-test-alma-9'
    if podman.image_exists(name):
        podman.remove_image(name)
    with TempDir() as tmp:
        p = tmp / "Containerfile"
        podman.assure_context(tmp / "Containerfile", 'FROM almalinux:9')
        podman.build_image(name, p)
        assert podman.image_exists(name)
        assert podman.remove_image(name)
