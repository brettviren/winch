#!/usr/bin/env python
'''
Podman interface for winch.

Notes for podman usage:

- On some systems, entries are needed for non-native accounts.  Eg:

  $ grep bviren /etc/subuid /etc/subgid
  /etc/subuid:bviren:100000:65536
  /etc/subgid:bviren:100000:65536

- To use local storage (eg to avoid NFS $HOME)

  $ export CONTAINERS_STORAGE_CONF=/path/to/fast/disk/containers/storage.conf
  $ cat $CONTAINERS_STORAGE_CONF 
  [storage]
  driver = "vfs"
  graphroot = "/path/to/fask/disk/containers/storage"
  rootless_storage_path = "/path/to/fask/disk/containers/storage"

- Be careful of having large enough temp dir.

  $ export TMPDIR=/path/to/big/disk/tmp

'''
from pathlib import Path
from .util import which, assure_file

def assure_context(containerfile, text=None, files=()):
    '''
    Construct a context directory holding a Containerfile.

    - containerfile :: a string or Path to a Containerfile
    - text :: desired Containerfile content.
    - files :: list of additional file paths to copy into the Containerfile dir.

    The containerfile and parent directories will be created as needed.  If text
    is written to the Containerfile unless it already matches in which case
    Containerfile is not updated.
    '''
    assure_file(containerfile, text)

    path = Path(containerfile)
    for one in files:
        shutil.copy(one, path.parent)


def pull_image(name):
    '''
    Pull named image.  Return hash.
    '''
    podman = which("podman")
    out = podman(['pull', name], capture_output=True)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return out.stdout.decode().strip()

    

def remove_image(name):
    '''
    Remove named image if it exists

    Return True if image actually removed (False if it was not there to start).
    '''
    if not image_exists(name):
        return False

    podman = which("podman")
    return podman(["image","rm",name])


def build_image(name, containerfile, *args):
    '''
    Build from containerfile with given name.

    Any args will be passed to "podman build"
    '''
    cfpath = Path(containerfile)
    context = str(cfpath.parent)
    podman = which("podman")
    return podman(["build"] + list(args) + ["-t", name, context])


def tag_image(name, tag):
    '''
    Add an additional tag to an existing image.

    Equivalent to "podman tag NAME TAG".  If TAG already names another image, it
    is reassigned to NAME (podman moves the tag), so re-running winch overrides
    any prior tag of the same name.
    '''
    podman = which("podman")
    return podman(["tag", name, tag])


def label_args(labels):
    '''
    Return a "podman build" argument list applying the given labels.

    - labels :: a mapping of label name to value.

    Returns a flat list such as ['--label', 'winch.layer=spack', '--label',
    'winch.digest=abc...'].  Values are stringified and arguments are emitted in
    sorted-key order for deterministic output.  Args are passed to podman as a
    list (not via a shell), so values need no shell quoting.
    '''
    out = list()
    for key in sorted(labels):
        out += ["--label", f'{key}={labels[key]}']
    return out


def image_labels(name):
    '''
    Return the dict of labels on an image (empty dict if none/no labels).
    '''
    import json
    podman = which("podman")
    out = podman(['inspect', '--format', '{{json .Config.Labels}}', name],
                 capture_output=True)
    text = out.stdout.decode().strip()
    if not text or text == "null":
        return dict()
    return json.loads(text) or dict()


def image_exists(name):
    '''
    Return True only if image exists.
    '''
    podman = which("podman")
    return 0 == podman(['image', 'exists', name], check=False).returncode


def create_container(image, name=None):
    '''
    Create a container from image with name, if given.  Return container ID.
    '''
    if not image_exists(image):
        raise IOError(f'no such image: {image}')
    args = ['create']
    if name:
        args += ['--name',name]
    args += [image]

    podman = which("podman")
    got = podman(args, capture_output=True)
    return got.stdout.decode().strip()


def remove_container(cid):
    '''
    Remove a contain by a container ID or name.
    '''
    podman = which("podman")
    podman(["rm", cid])


def container_copy(cid, path, outpath='.'):
    '''
    Copy file at path from running container cid to outapath.
    '''
    podman = which("podman")
    podman(["cp", f'{cid}:{path}', outpath])


def run_image(run_argv):
    '''
    Exec "podman run" with the given argument list.

    - run_argv :: the token list that follows "podman run" (options, image,
      command).

    This replaces the current process with podman (os.execvp) so an interactive
    container ("-it") attaches directly to the terminal and signals pass through.
    '''
    import os
    import shutil
    podman = shutil.which("podman")
    if podman is None:
        raise FileNotFoundError('no such executable "podman"')
    os.execvp(podman, [podman, "run"] + list(run_argv))


def image_copy(image, path, outpath='.'):
    '''
    Extract path from image an copy it to host outpath
    '''
    cid = create_container(image)
    container_copy(cid, path, outpath)
    remove_container(cid)

    
