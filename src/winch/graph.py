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

from .util import debug, digest, outer_product, self_format, product
import networkx as nx

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
            else:
                adat = dict(adat)
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

        kpaths = list()
        kleaves = [n for n in self.K.nodes() if self.K.out_degree(n) == 0]
        for knode in [n for n in self.K.nodes() if self.K.in_degree(n) == 0]:
            kpaths += nx.all_simple_paths(self.K, knode, kleaves)

        self.I = nx.DiGraph()
        for kpath in kpaths:
            idats_on_path = list()
            for knum, knode in enumerate(kpath):
                parent_idats = None
                if knum:
                    parent_idats = idats_on_path[knum-1]
                adats = self._generate_adata(kpath[:knum+1])
                idats = self._generate_idata(adats, parent_idats)
                idats_on_path.append(idats)

        
