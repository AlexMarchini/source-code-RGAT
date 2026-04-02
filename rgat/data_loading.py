"""
rgat.data_loading – Load and structurally validate the graph JSON.

Reads the JSON produced by ``graph_builder``, verifies top-level schema,
and returns typed dicts ready for downstream feature validation and graph
construction.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from rgat.config import VALID_EDGE_TYPES, VALID_NODE_TYPES


# ── Public types ───────────────────────────────────────────────────────
RawNode = Dict[str, Any]   # {"id": str, "type": str, "features": dict}
RawEdge = Dict[str, str]   # {"source": str, "type": str, "target": str}


def load_json(json_path: str | Path) -> Tuple[Dict[str, Any], List[RawNode], List[RawEdge]]:
    """Load the graph JSON and run structural validation.

    Returns
    -------
    metadata : dict
        The ``"metadata"`` block from the JSON.
    nodes : list[RawNode]
        Validated node dicts (each has ``id``, ``type``, ``features``).
    edges : list[RawEdge]
        Validated edge dicts (each has ``source``, ``type``, ``target``).

    Raises
    ------
    FileNotFoundError
        If *json_path* does not exist.
    ValueError
        If any structural invariant is violated.
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"Graph JSON not found: {path.resolve()}")

    print(f"[data_loading] Loading {path} …", flush=True)
    t0 = time.perf_counter()
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    elapsed = time.perf_counter() - t0
    print(f"[data_loading] Loaded in {elapsed:.1f}s", flush=True)

    # ── Top-level keys ──
    _require_keys(raw, {"metadata", "nodes", "edges"}, context="top-level JSON")

    metadata: Dict[str, Any] = raw["metadata"]
    nodes: List[RawNode] = raw["nodes"]
    edges: List[RawEdge] = raw["edges"]

    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("'nodes' and 'edges' must be JSON arrays")

    # ── Validate nodes ──
    seen_ids: set[str] = set()
    for i, node in enumerate(nodes):
        _require_keys(node, {"id", "type"}, context=f"nodes[{i}]")
        nid = node["id"]
        ntype = node["type"]

        if not isinstance(nid, str) or not nid:
            raise ValueError(f"nodes[{i}]: 'id' must be a non-empty string, got {nid!r}")
        if nid in seen_ids:
            raise ValueError(f"nodes[{i}]: duplicate node id {nid!r}")
        seen_ids.add(nid)

        if ntype not in VALID_NODE_TYPES:
            raise ValueError(
                f"nodes[{i}] (id={nid!r}): unexpected node type {ntype!r}. "
                f"Expected one of {sorted(VALID_NODE_TYPES)}"
            )

        # features must be a dict (may be absent — that will fail in schema validation)
        if "features" not in node or not isinstance(node.get("features"), dict):
            raise ValueError(
                f"nodes[{i}] (id={nid!r}): 'features' key is missing or not a dict"
            )

    # ── Validate edges ──
    for i, edge in enumerate(edges):
        _require_keys(edge, {"source", "type", "target"}, context=f"edges[{i}]")
        etype = edge["type"]
        if etype not in VALID_EDGE_TYPES:
            raise ValueError(
                f"edges[{i}]: unexpected edge type {etype!r}. "
                f"Expected one of {sorted(VALID_EDGE_TYPES)}"
            )
        if edge["source"] not in seen_ids:
            raise ValueError(
                f"edges[{i}]: source {edge['source']!r} is not a known node id"
            )
        if edge["target"] not in seen_ids:
            raise ValueError(
                f"edges[{i}]: target {edge['target']!r} is not a known node id"
            )

    print(f"[data_loading] Structural validation passed: "
          f"{len(nodes):,} nodes, {len(edges):,} edges", flush=True)

    return metadata, nodes, edges


# ── Helpers ────────────────────────────────────────────────────────────

def _require_keys(obj: dict, keys: set[str], *, context: str) -> None:
    missing = keys - set(obj.keys())
    if missing:
        raise ValueError(f"{context}: missing required keys {sorted(missing)}")
