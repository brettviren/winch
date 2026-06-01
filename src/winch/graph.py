#!/usr/bin/env python
'''
The winch container image layer graph.

## Overview

The winch graph has three node types (K, A and I) and four edge types (K, A, I
and M).  Conceptually, removal of all "M" edges allows factoring the graph into
three disconnected graphs each consisting of only K, only A or only I nodes and
edges.  The K-graph is a directed graph with both splits and joins allowed.  The
A-graph and I-graph are trees with splits allowed but no joins.  

The K, A and I nodes represent a progression from more to less ambiguity of
parameters.

- K-node represents a "kind" of container image layer and is most ambiguous
  (spans variants) along two "dimensions".  First, a K-node may have zero or
  more a parent K-nodes.  Each K-parent represents all possible image layers
  that may be used as the FROM for all layers that the K-node can generate.
  Second, a K-node may have zero or more parameters that are list-of-string.
  The K-node represents all possible parameter sets formed as the outer product
  of these parameters.

- A-node represents a more concrete but still abstract version of a K-node where
  one from the set of possible K-parents has been selected.  Each A-node "made"
  by a K-node is represented by an M-edge.  Each A-node is also the head of an
  A-edge linking it to an A-parent which was made by the selected K-parent.  Any
  list-of-string parameters of the K-node are left ambiguous.

- I-node represents a concrete version of an A-node where any list-of-string
  parameters have been resolved.  All string parameters are interpolated.  The
  I-node "made" from an A-node is connected by an M-edge and the I-parent made
  from the A-parent is connected by an I-edge.

'''

import re

from .util import debug, digest, outer_product, self_format, product, find_unresolved
from .config import ConfigError
from .recipe import _resolve_caps
import networkx as nx


# Prefix for auto-generated (digest-tagged) new-paradigm image names.
WINCH_IMAGE_PREFIX = "localhost/winch"
# Number of hex digest characters used in an auto-generated image tag.
WINCH_IMAGE_DIGEST_LEN = 12
# Instance-data keys created by build_instance that are not layer variables.
INSTANCE_STRUCTURAL_KEYS = ("kind", "parent", "containerfile", "image")

# Match {layer.NAME.VAR} — negative lookbehind prevents matching {{layer.
_LAYER_REF_RE = re.compile(
    r'(?<!\{)\{layer\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\}'
)
# Match {requires.NAME.VAR} — same escaped-brace guard.
_REQUIRES_REF_RE = re.compile(
    r'(?<!\{)\{requires\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\}'
)


def _expand_cross_layer_refs(idata, current_layer_name, recipe_name,
                              layer_obj, prior_stack, raw_vars):
    '''
    Pre-expand {layer.NAME.VAR} and {requires.NAME.VAR} references in idata.

    Must be called before self_format so that the expanded literals are then
    available to the normal {varname} / {parent[varname]} resolution pass.
    Mutates idata string values in place.  Raises ConfigError on any
    unresolvable cross-layer reference.

    prior_stack  — list of (layer_name, resolved_vars_dict, provides_set) for
                   every layer that has already been processed (base-first).
    raw_vars     — the merged-but-unformatted variable map for the current
                   layer; used only to format its requires list for matching.
    '''
    prior_by_name = {name: pvars for name, pvars, _ in prior_stack}

    def expand_layer(m):
        lname, vname = m.group(1), m.group(2)
        if lname not in prior_by_name:
            raise ConfigError(
                f'recipe "{recipe_name}" layer "{current_layer_name}": '
                f'{{layer.{lname}.{vname}}} references "{lname}" which is '
                f'not a prior layer in the stack')
        pvars = prior_by_name[lname]
        if vname not in pvars:
            raise ConfigError(
                f'recipe "{recipe_name}" layer "{current_layer_name}": '
                f'{{layer.{lname}.{vname}}} - layer "{lname}" has no '
                f'variable "{vname}"')
        return str(pvars[vname])

    # Build requires_vars: identifier-capable requirement → providing layer vars.
    # Scan prior_stack forward (base → current); later entries overwrite earlier
    # ones so the highest layer in the stack wins when multiple layers provide
    # the same capability.
    formatted_requires = set(_resolve_caps(layer_obj.requires, raw_vars))
    requires_vars = {}
    for _pname, pvars, pprovides in prior_stack:
        for cap in pprovides:
            if cap in formatted_requires and cap.isidentifier():
                requires_vars[cap] = pvars

    def expand_requires(m):
        rname, vname = m.group(1), m.group(2)
        if rname not in formatted_requires:
            raise ConfigError(
                f'recipe "{recipe_name}" layer "{current_layer_name}": '
                f'{{requires.{rname}.{vname}}} - "{rname}" is not in '
                f'this layer\'s requires list')
        if rname not in requires_vars:
            raise ConfigError(
                f'recipe "{recipe_name}" layer "{current_layer_name}": '
                f'{{requires.{rname}.{vname}}} - no prior layer provides '
                f'"{rname}"')
        pvars = requires_vars[rname]
        if vname not in pvars:
            raise ConfigError(
                f'recipe "{recipe_name}" layer "{current_layer_name}": '
                f'{{requires.{rname}.{vname}}} - the providing layer has '
                f'no variable "{vname}"')
        return str(pvars[vname])

    for k in list(idata.keys()):
        v = idata[k]
        if not isinstance(v, str):
            continue
        if _LAYER_REF_RE.search(v):
            idata[k] = _LAYER_REF_RE.sub(expand_layer, v)
            v = idata[k]
        if _REQUIRES_REF_RE.search(v):
            idata[k] = _REQUIRES_REF_RE.sub(expand_requires, v)


def instance_labels(node, idata, provides=()):
    '''
    Build the winch.* provenance label mapping for one built instance.

    - node: the instance's content digest (full, e.g. the I-graph node id).
    - idata: the formatted instance data.
    - provides: an iterable of (already self-formatted) capability strings the
      layer provides.

    Returns a dict suitable for podman.label_args():
      winch.layer    -> idata["kind"]
      winch.digest   -> node
      winch.var.<k>  -> value, for each resolved layer variable
      winch.provides -> comma-joined provides (only when non-empty)
    '''
    labels = {
        "winch.layer": idata.get("kind", ""),
        "winch.digest": node,
    }
    for key, value in idata.items():
        if key in INSTANCE_STRUCTURAL_KEYS:
            continue
        labels[f"winch.var.{key}"] = value
    provides = list(provides)
    if provides:
        labels["winch.provides"] = ",".join(provides)
    return labels


def _content_digest(idata):
    '''
    Digest of an instance's data excluding its own "image" name.

    The image name is derived from this digest, so it must not feed it.  A
    child's data embeds its parent (including the parent's image name), so the
    digest chains down the stack and identical stack prefixes dedupe.
    '''
    return digest({k: v for k, v in idata.items() if k != "image"})


def build_instance(layer, variables, parent_idata):
    '''
    Build the unformatted instance-data dict for one layer in a stack.

    - layer: a config.Layer.
    - variables: the resolved variable map for this layer (defaults + overrides),
      which may include an explicit "image" to override digest-based naming.
    - parent_idata: the formatted instance data of the previous layer, or None
      for the base layer.

    Capability tags (provides/requires) are intentionally NOT placed in the
    instance data: they are build-irrelevant metadata and must not affect the
    content digest (and thus the image identity).
    '''
    idata = dict(variables)
    idata["kind"] = layer.name
    if parent_idata is not None:
        idata["parent"] = parent_idata
    if layer.containerfile is not None:
        idata["containerfile"] = layer.containerfile
    elif layer.body is not None:
        idata["containerfile"] = "FROM {parent[image]}\n" + layer.body
    return idata


def generate_instances(layers, resolved_recipes, graph=None):
    '''
    Build I-graph instance chains for the new paradigm.

    - layers: dict of layer name to config.Layer.
    - resolved_recipes: iterable of recipe.ResolvedRecipe.
    - graph: an existing Graph to extend, or None to start fresh.

    Returns the Graph whose .I holds the deduped union of all recipe chains.
    Raises ConfigError if a buildable layer has unresolved "{...}" markup.
    '''
    if graph is None:
        graph = Graph()
    I = graph.I

    for rr in resolved_recipes:
        parent_idata = None
        parent_inode = None
        # Accumulate (layer_name, resolved_vars, provides_set) for cross-layer refs.
        prior_stack = []

        for layer_name in rr.stack:
            layer = layers[layer_name]
            idata = build_instance(layer, rr.layer_vars[layer_name], parent_idata)

            # Pre-expand {layer.NAME.VAR} and {requires.NAME.VAR} before the
            # normal self_format pass handles {varname} / {parent[varname]}.
            _expand_cross_layer_refs(
                idata, layer_name, rr.name, layer,
                prior_stack, rr.layer_vars[layer_name])

            # Capture original string values (escapes intact) for the resolution
            # check, then self-format in place.
            originals = {k: v for k, v in idata.items() if isinstance(v, str)}
            self_format(idata)

            unresolved = find_unresolved(originals, idata)
            if unresolved:
                detail = "; ".join(f'{k}: {refs}' for k, refs in sorted(unresolved.items()))
                raise ConfigError(
                    f'recipe "{rr.name}" layer "{layer_name}" has unresolved '
                    f'markup: {detail}')

            inode = _content_digest(idata)
            if idata.get("image") is None:
                idata["image"] = (f'{WINCH_IMAGE_PREFIX}/{layer_name}'
                                  f':{inode[:WINCH_IMAGE_DIGEST_LEN]}')

            if inode not in I:
                I.add_node(inode, **idata)
            if parent_inode is not None:
                I.add_edge(parent_inode, inode)

            # Record this layer's resolved variables and formatted provides for
            # {layer.NAME.VAR} / {requires.NAME.VAR} resolution in later layers.
            resolved_vars = {k: v for k, v in idata.items()
                             if k not in INSTANCE_STRUCTURAL_KEYS}
            provides = set(_resolve_caps(layer.provides, resolved_vars))
            prior_stack.append((layer_name, resolved_vars, provides))

            parent_idata = idata
            parent_inode = inode

    return graph

class Graph:

    def __init__(self, **knodes):
        if knodes is None:
            return
        self.initialize(**knodes)

    def nodes(self, ntype='I'):
        if ntype == 'I':
            return self.I.nodes.data()
        if ntype == 'K':
            return self.K.nodes.data()
        raise ValueError(f'unknown ntype: "{ntype}"')
        
    def data(self, node, ntype='I'):
        g = getattr(self, ntype)
        return g.nodes[node]

    def _generate_adata(self, kpath):
        kind = kpath[-1]
        adata = dict(self.K.nodes[kind])
        adata['kpath'] = tuple(kpath)
        adata['kind'] = kind
        if len(kpath) > 1:
            adata['parent_kind'] = kpath[-2]
        return outer_product(adata)

    def _generate_idata(self, adats, iparentdats=None):

        if not iparentdats:
            iparentdats = [None]
        ret = list()
        for adat, iparentdat in product(adats, iparentdats):
            if iparentdat:
                adat = dict(adat, parent=iparentdat)
                # print(f'{iparentdat=}')
            else:
                adat = dict(adat)
            # print(f'{adat.keys()}')
            idat = self_format(adat)

            # An I-node can be seen multiple times when it comes from a root
            # K-node seen in different paths.
            inode = digest(idat)
            if inode not in self.I:
                self.I.add_node(inode, **idat)

            if iparentdat:
                ipnode = digest(iparentdat)
                self.I.add_edge(ipnode, inode)
            ret.append(idat)
        return ret


    def kpaths(self):
        '''
        Return list-of-tuple of all K-graph paths.
        '''
        kpaths = list()
        kleaves = [n for n in self.K.nodes() if self.K.out_degree(n) == 0]
        for knode in [n for n in self.K.nodes() if self.K.in_degree(n) == 0]:
            kpaths += tuple(nx.all_simple_paths(self.K, knode, kleaves))
        return kpaths


    def initialize(self, **knodes):
        '''
        Initialize the graph with mapping from kind name to kind parameters.
        '''
        self.K = nx.DiGraph()
        for knode, kdata in knodes.items():
            self.K.add_node(knode, **kdata)

        for knode, kdata in knodes.items():
            pks = kdata.get('parent_kind', None)
            if not pks:
                continue
            if isinstance(pks, str):
                pks = [pks]
            for pk in pks:
                self.K.add_edge(pk, knode)

        self.I = nx.DiGraph()
        for kpath in self.kpaths():
            idats_on_path = list()
            for knum, knode in enumerate(kpath):
                parent_idats = None
                if knum:
                    parent_idats = idats_on_path[knum-1]
                adats = self._generate_adata(kpath[:knum+1])
                idats = self._generate_idata(adats, parent_idats)
                idats_on_path.append(idats)

        
    def from_kpath(self, kpath):
        '''
        Return list of lists of I-nodes generated along K-graph path.

        A path may be represented as a string as a comma-separated list of K-nodes.
        '''
        # normalize
        if isinstance(kpath, str):
            kpath = kpath.split(",")
        kpath = tuple(kpath)
        kpath_str = ','.join(kpath)

        ret = [list() for p in kpath]
        ndeep = len(kpath)
        for inode, idata in self.I.nodes.data():
            maybe = idata['kpath']
            if len(maybe) > ndeep:
                continue
            maybe_str = ','.join(maybe)
            if len(maybe_str) > len(kpath_str):
                continue
            if kpath_str[:len(maybe_str)] == maybe_str:
                ret[len(maybe)-1].append(inode)
        return ret
            
    def from_kind(self, kind):
        '''
        Return all I-nodes of a kind regardless of K-graph path.
        '''
        return [n for n,d in self.I.nodes.data() if d['kind'] == kind]

    def ipath(self, ileaf):
        '''
        Return ordered dependency list of the I-nodes on which ileaf
        depends, ending with ileaf.
        '''
        ret = [ileaf]
        while self.I.in_degree(ret[-1]):
            suc = list(self.I.predecessors(ret[-1]))
            if len(suc) > 1:
                raise ValueError(f'Malformed I-graph: {ret[-1]} has multiple parents {suc}')
            ret.append(suc[0])
        ret.reverse()
        return ret

