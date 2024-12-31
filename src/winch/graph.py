#!/usr/bin/env python
'''
Functions for winch container image layer graph.

## Overview

The graph is directed with three types of nodes and four types of edges.  The
node types are:

- K node represents a "kind" of container image layer.  K node parameters may be
  ambiguous over a list of variant values.

- A node represents an abstract version of a K node with ambiguity removed by
  selecting exactly one from each variant.

- I node represents a concrete version of A where all string parameters are
  interpolated.

The graph has the following edges:

- K edge joins K-K nodes as governed by a K node `parent_kind` parameter.  A "K
  subgraph" is embedded and may be formed by selecting K nodes and K edges.
  This subgraph may have both joins and splits.

- I edge joins I-I nodes as governed by the generation algorithm.  An tail of
  the I edge records the layer on which the head of an I node is built.  An "I
  subgraph" is embedded and may be formed by selecting I nodes and I edges.

- A edge joins K-A nodes and represents the removal of list parameter ambiguity.

- M edge joins A-I nodes and represents the "making" of an I node from an A
  node.

'''

from .util import debug, digest, outer_product, self_format, product
import networkx as nx

class Graph:

    def __init__(self, knodes = None):
        self.graph = nx.DiGraph()
        if knodes is None:
            return
        self.update(knodes)

    def add_node(self, ntype, name, **data):
        '''
        Add a node to the graph, return its ID

        This asserts the policy:

        - No adding of nodes of the same identity.

        - ntype is a node type code: K, A or I.

        - name provides fodder for a node identity and is generally assumed to
          be a K-node name regardless of ntype.

        - The node identity is set as:

          - K-node uses "name" unchanged.

          - A-node uses "A_<name>_<hash>" or data[anode]

          - I-node uses "<name>_<hash>" or data[inode]

        When node name comes from data for A and I types, the string is
        formatted against a '{digest}' giving a hash of data.  If a node name is
        provided in this way, be cautious that it is unique.

        The data will be augmented to the following:

        - nodetype :: the ntype letter.
        '''
        ntype = ntype[0].upper()
        if ntype not in "KAI":
            raise ValueError(f'unsupported node type: {ntype}')
        data['nodetype'] = ntype

        if ntype == 'K':
            ident = name
        else:
            dig = digest([ntype, name, data])
            if ntype == 'A':
                ident = f'A_{name}_{dig}'
            else:               # I
                ident = data.get('inode', f'{name}_{dig}').format(digest=dig)

        if ident in self.graph:
            raise ValueError(f'node {ident} already in graph')

        self.graph.add_node(ident, **data)
        return ident


    def add_edge(self, etype, tail, head, **data):
        '''
        Add edge from tail to head of given etype (K,A,M,I)

        The etype is added to data as label
        '''
        etype = etype[0].upper()
        self.graph.add_edge(tail, head, label=etype, **data)


    def _updateK(self, knodes):
        '''
        Update the "K" subgraph with mapping from kind node ID to its parameters.
        '''
        for knode, kdata in knodes.items():
            self.add_node('K', knode, kind=knode, **kdata) # name is ID for K nodes
            self._updateA(knode)

        for knode, kdata in knodes.items():
            pks = kdata.get('parent_kind', None)
            if not pks:
                continue
            if isinstance(pks, str):
                pks = [pks]
            for pk in pks:
                self.add_edge('K', pk, knode)

    def _updateA(self, knode):
        '''
        Generate A nodes from existing a K-node and add them to graph.
        '''
        kdata = self.graph.nodes[knode]
        for adat in outer_product(kdata):
            adat = self_format(adat)
            aname = self.add_node('A', knode, **adat)
            self.add_edge('A', knode, aname)


    def get_anodes(self, knode):
        '''
        Return A-nodes of K-node.
        '''
        return self.get_successors(knode, 'A')


    def get_inodes(self, knode):
        '''
        Return the I-nodes generated from A-nodes generated from K-node.
        '''
        inodes = list()
        for anode in self.get_anodes(knode):
            inodes += self.get_successors(anode, 'I')
        inodes = list(set(inodes))
        inodes.sort()
        return inodes


    def _updateI(self, knode):
        '''
        Generate the I nodes from the K-node's A nodes and any I-nodes from
        the K-node's K-parent.
        '''
        # get all I-nodes of K-parent to given K-node
        inodes = list()
        for kparent in self.get_predecessors(knode, 'K'):
            inodes += self.get_inodes(kparent)
        if not inodes:
            inodes = [None]     # placeholder for product()

        # get all A-nodes of this K node
        anodes = self.get_anodes(knode)

        for anode, inode in product(anodes, inodes):
            adat = self.graph.nodes[anode]
            if inode is not None:
                adat['parent'] = self.graph.nodes[idat]
            idat = self_format(adat)
            iname = self.add_node('I', knode, **idat)
            self.add_edge('M',anode, iname)
            if inode is not None:
                self.add_edge('I', inode, iname)

    def update(self, knodes):
        '''
        Update the graph with mapping from kind name to kind parameters.
        '''
        self._updateK(knodes)
        for knode in knodes:
            self._updateI(knode)
        
    def get_roots(self):
        '''
        Return nodes which have no predecessors
        '''
        roots = list()
        for node in self.graph.nodes:
            if not self.get_predecessors(node):
                roots.append(node)
        return node

    def get_successors(self, node: str, label: str|None = None):
        '''
        Return the successor (child) nodes.

        If label is given, limit to those reached along an edge with that label.
        '''
        nodes = self.graph.successors(node)
        if label is None:
            return nodes
        return [n for n in nodes if self.graph.edges[node,n]["label"] == label]
        
    def get_predecessors(self, node: str, label: str|None = None):
        '''
        Return the predecessor (parent) nodes.

        If label is given, limit to those reached along an edge with that label.
        '''
        nodes = self.graph.predecessors(node)
        if label is None:
            return nodes
        return [n for n in nodes if self.graph.edges[n,node]["label"] == label]

    def get_ichain(self, inode):
        '''
        Return list of I-nodes by walking I-parents along I-edge starting at inode.

        List ordered parents-first, ends in inode.
        '''

        ret = [inode]
        while True:
            parents = self.get_predecessors(inode, 'I')
            nparents = len(parents)
            if nparents == 0:
                break
            if nparents > 1:
                raise RuntimeError(f'corrupt graph, I-node "{inode}" has {nparents} parent I-nodes')
            inode = parents[0]
            ret.append(inode)
        ret.reverse()
        return ret

        
