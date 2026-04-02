"""
rgat.data_cleaning – Pre-processing filters to improve graph quality.

Applied between ``validate_features()`` and ``encode_texts()`` to remove
noisy patterns discovered during the data quality audit:

1. **File node collapse** – ``file`` nodes are structural intermediaries with
   no semantic edges.  Replace ``repo → CONTAINS_FILE → file → IMPLEMENTS_MODULE
   → module`` chains with direct ``repo → CONTAINS_MODULE → module`` edges.
2. **Empty __init__ hub removal** – 44 % of ``IMPORTS_MODULE`` edges point to
   ``__init__`` packages that define zero classes / functions.  These create
   uninformative attention sinks.
3. **Self-loop cleanup** – Remove ``IMPORTS_MODULE`` and ``INHERITS`` self-loops
   (module importing itself, class inheriting itself — data bugs).
4. **CALLS degree capping** – A handful of hub functions absorb > 16 % of all
   ``CALLS`` edges, collapsing attention to near-uniform.  Cap incoming edges
   per target node via random subsampling.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple


def clean_graph(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, str]],
    *,
    calls_degree_cap: int = 100,
    remove_init_hub_edges: bool = True,
    remove_file_nodes: bool = True,
    seed: int = 42,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Apply data-quality filters to the raw graph.

    Parameters
    ----------
    nodes, edges : list[dict]
        Raw validated JSON data.
    calls_degree_cap : int
        Maximum incoming ``CALLS`` edges per target function (0 = no cap).
    remove_init_hub_edges : bool
        Drop ``IMPORTS_MODULE`` edges pointing to empty ``__init__`` modules.
    remove_file_nodes : bool
        Collapse ``file`` nodes into ``CONTAINS_MODULE`` edges.
    seed : int
        RNG seed for reproducible ``CALLS`` sub-sampling.

    Returns
    -------
    nodes, edges : tuple[list, list]
        Cleaned lists (shallow copies — originals are not modified).
    """
    rng = random.Random(seed)
    nodes = list(nodes)   # shallow copy
    edges = list(edges)

    print(f"\n{'=' * 65}")
    print("  DATA CLEANING")
    print(f"{'=' * 65}")
    print(f"  Input: {len(nodes):,} nodes, {len(edges):,} edges")

    # ── 0. Pre-compute __init__ module set (needs file nodes) ──────────
    init_module_ids: Set[str] = set()
    if remove_init_hub_edges:
        init_module_ids = _find_init_module_ids(nodes, edges)

    # ── 1. Collapse file nodes ─────────────────────────────────────────
    if remove_file_nodes:
        nodes, edges = _collapse_file_nodes(nodes, edges)

    # ── 2. Remove IMPORTS_MODULE edges to empty __init__ modules ───────
    if remove_init_hub_edges and init_module_ids:
        edges = _remove_init_hub_edges(nodes, edges, init_module_ids)

    # ── 3. Remove self-loops (IMPORTS_MODULE, INHERITS) ────────────────
    edges = _remove_self_loops(edges)

    # ── 4. Cap CALLS in-degree ─────────────────────────────────────────
    if calls_degree_cap > 0:
        edges = _cap_calls_indegree(edges, calls_degree_cap, rng)

    # ── Summary ────────────────────────────────────────────────────────
    node_counts = Counter(n["type"] for n in nodes)
    edge_counts = Counter(e["type"] for e in edges)
    print(f"\n  Result: {len(nodes):,} nodes, {len(edges):,} edges")
    for nt, c in sorted(node_counts.items()):
        print(f"    {nt:>10s}: {c:>7,} nodes")
    for et, c in sorted(edge_counts.items()):
        print(f"    {et:>20s}: {c:>7,} edges")
    print(f"{'=' * 65}\n")

    return nodes, edges


# ── Internal transforms ────────────────────────────────────────────────


def _find_init_module_ids(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, str]],
) -> Set[str]:
    """Identify module IDs that correspond to ``__init__.py`` files.

    Uses the ``file → IMPLEMENTS_MODULE → module`` linkage and the
    ``is_init`` flag on file nodes.  Must be called **before** file
    node collapse.
    """
    # Find files with is_init=True
    init_file_ids: Set[str] = set()
    for n in nodes:
        if n["type"] == "file" and n.get("features", {}).get("is_init", 0):
            init_file_ids.add(n["id"])

    # Map file → module via IMPLEMENTS_MODULE edges
    init_module_ids: Set[str] = set()
    for e in edges:
        if e["type"] == "IMPLEMENTS_MODULE" and e["source"] in init_file_ids:
            init_module_ids.add(e["target"])

    return init_module_ids


def _collapse_file_nodes(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Remove ``file`` nodes; bridge ``repo → CONTAINS_MODULE → module``.

    File nodes participate only in structural edges (CONTAINS_FILE,
    IMPLEMENTS_MODULE). Their removal saves 12 k nodes and ~48 k edges
    while preserving all semantic signal.
    """
    # Map file → repo  and  file → module
    file_to_repo: Dict[str, str] = {}
    file_to_module: Dict[str, str] = {}

    for e in edges:
        if e["type"] == "CONTAINS_FILE":
            file_to_repo[e["target"]] = e["source"]
        elif e["type"] == "IMPLEMENTS_MODULE":
            file_to_module[e["source"]] = e["target"]

    # Create deduplicated CONTAINS_MODULE edges
    contains_module: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for file_id, mod_id in file_to_module.items():
        repo_id = file_to_repo.get(file_id)
        if repo_id and (repo_id, mod_id) not in seen:
            contains_module.append({
                "source": repo_id,
                "type": "CONTAINS_MODULE",
                "target": mod_id,
            })
            seen.add((repo_id, mod_id))

    # Filter
    n_files = sum(1 for n in nodes if n["type"] == "file")
    nodes = [n for n in nodes if n["type"] != "file"]
    edges = [
        e for e in edges
        if e["type"] not in ("CONTAINS_FILE", "IMPLEMENTS_MODULE")
    ]
    edges.extend(contains_module)

    print(f"  [1] Collapsed {n_files:,} file nodes → "
          f"{len(contains_module):,} CONTAINS_MODULE edges")
    return nodes, edges


def _remove_init_hub_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, str]],
    init_module_ids: Set[str],
) -> List[Dict[str, str]]:
    """Drop ``IMPORTS_MODULE`` edges targeting empty ``__init__`` modules.

    Empty = ``num_classes_defined == 0 AND num_functions_defined == 0``.
    These package-level re-export hubs absorb ~44 % of import edges with
    no semantic signal, creating attention sinks.
    """
    # Filter to only empty init modules
    module_lookup = {n["id"]: n for n in nodes if n["type"] == "module"}
    empty_init_ids: Set[str] = set()
    for mid in init_module_ids:
        node = module_lookup.get(mid)
        if node:
            feats = node.get("features", {})
            is_empty = (
                feats.get("num_classes_defined", 0) == 0
                and feats.get("num_functions_defined", 0) == 0
            )
            if is_empty:
                empty_init_ids.add(mid)

    before = len(edges)
    edges = [
        e for e in edges
        if not (e["type"] == "IMPORTS_MODULE" and e["target"] in empty_init_ids)
    ]
    removed = before - len(edges)
    print(f"  [2] Removed {removed:,} IMPORTS_MODULE edges to "
          f"{len(empty_init_ids):,} empty __init__ modules")
    return edges


def _remove_self_loops(edges: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Remove self-loops from ``IMPORTS_MODULE`` and ``INHERITS``."""
    loop_types = {"IMPORTS_MODULE", "INHERITS"}
    before = len(edges)
    edges = [
        e for e in edges
        if not (e["type"] in loop_types and e["source"] == e["target"])
    ]
    removed = before - len(edges)
    print(f"  [3] Removed {removed} self-loops (IMPORTS_MODULE + INHERITS)")
    return edges


def _cap_calls_indegree(
    edges: List[Dict[str, str]],
    cap: int,
    rng: random.Random,
) -> List[Dict[str, str]]:
    """Sub-sample incoming ``CALLS`` edges for hub target nodes.

    For each target function with in-degree > *cap*, randomly keep *cap*
    edges and discard the rest.  This prevents a handful of utility
    functions from dominating attention softmax.
    """
    calls = [e for e in edges if e["type"] == "CALLS"]
    rest = [e for e in edges if e["type"] != "CALLS"]

    # Group by target
    by_target: Dict[str, List[Dict]] = defaultdict(list)
    for e in calls:
        by_target[e["target"]].append(e)

    kept: List[Dict] = []
    n_hubs = 0
    n_dropped = 0
    for _target, group in by_target.items():
        if len(group) > cap:
            rng.shuffle(group)
            kept.extend(group[:cap])
            n_hubs += 1
            n_dropped += len(group) - cap
        else:
            kept.extend(group)

    print(f"  [4] Capped CALLS in-degree at {cap}: "
          f"{n_hubs} hub nodes, {n_dropped:,} edges dropped")
    return rest + kept
