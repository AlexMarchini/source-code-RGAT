"""
graph_builder.model – Data model for the code graph.

Provides Node, Edge, and Graph dataclasses with deterministic JSON
serialisation (sorted by id / source+type+target for stable diffs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Union


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class Node:
    """A single graph node.

    Attributes:
        id:       Deterministic, globally-unique identifier (see ID schema).
        type:     One of repo | file | module | class | function | symbol.
        features: Optional numeric / boolean feature dict for GNN training.
    """

    id: str
    type: str
    features: Dict[str, Union[int, float, bool, str]] = field(
        default_factory=dict, compare=False, repr=False,
    )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "type": self.type}
        if self.features:
            d["features"] = self.features
        return d


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class Edge:
    """A directed edge (relation) in the graph.

    Attributes:
        source: Node id of the tail.
        type:   Relation label (e.g. CALLS, INHERITS).
        target: Node id of the head.
    """

    source: str
    type: str
    target: str

    def to_dict(self) -> Dict[str, str]:
        return {"source": self.source, "type": self.type, "target": self.target}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

@dataclass
class Graph:
    """A heterogeneous directed graph with typed nodes and edges.

    ``to_dict()`` and ``write_json()`` guarantee deterministic output by
    sorting nodes by their *id* and edges by *(source, type, target)* before
    serialisation.
    """

    metadata: Dict[str, Any] = field(default_factory=dict)
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)

    # -- helpers for building ------------------------------------------------

    def add_node(self, node: Node) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return the graph as a plain dict matching the JSON schema.

        Nodes are sorted by *id*; edges by *(source, type, target)* so that
        the output is deterministic and yields stable diffs.
        """
        sorted_nodes = sorted(self.nodes, key=lambda n: n.id)
        sorted_edges = sorted(self.edges, key=lambda e: (e.source, e.type, e.target))
        return {
            "metadata": self.metadata,
            "nodes": [n.to_dict() for n in sorted_nodes],
            "edges": [e.to_dict() for e in sorted_edges],
        }

    def write_json(self, path: str | Path) -> None:
        """Serialise the graph to a JSON file at *path*.

        Uses ``indent=2`` and ``sort_keys=True`` inside nested dicts for
        maximum readability and determinism.
        """
        data = self.to_dict()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False, ensure_ascii=False)
            fh.write("\n")  # trailing newline

    # -- summary -------------------------------------------------------------

    def summary(self) -> str:
        """Return a human-readable summary of node/edge counts by type."""
        from collections import Counter

        node_counts = Counter(n.type for n in self.nodes)
        edge_counts = Counter(e.type for e in self.edges)
        lines = ["Graph summary", "============="]
        lines.append(f"  Total nodes: {len(self.nodes)}")
        for t in sorted(node_counts):
            lines.append(f"    {t:12s}: {node_counts[t]}")
        lines.append(f"  Total edges: {len(self.edges)}")
        for t in sorted(edge_counts):
            lines.append(f"    {t:20s}: {edge_counts[t]}")
        return "\n".join(lines)
