"""
rgat.graph_construction – Build a PyG ``HeteroData`` object from raw data.

Responsibilities:
  • Per-type node indexing (string id → integer index)
  • Feature tensor assembly (scalar + sentence embedding + leiden embedding id)
  • Edge grouping by inferred ``(src_type, relation, dst_type)`` triplets
  • Reverse edge generation for message passing
  • Dataset summary printing
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

import torch
from torch import Tensor

from torch_geometric.data import HeteroData

from rgat.config import REQUIRED_SCALAR_FEATURES, RGATConfig

import math


# ── Public return type ─────────────────────────────────────────────────
NodeIndex = Dict[str, Dict[str, int]]   # {node_type: {node_id: int_index}}


def build_hetero_data(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, str]],
    text_embeddings: Dict[str, Tensor],
    config: RGATConfig,
) -> Tuple[HeteroData, NodeIndex]:
    """Construct a ``HeteroData`` graph from validated raw data.

    Parameters
    ----------
    nodes : list[dict]
        Validated node dicts.
    edges : list[dict]
        Validated edge dicts.
    text_embeddings : dict[str, Tensor]
        Sentence embeddings per node type, ordered consistently with
        the node list (i.e. the i-th row corresponds to the i-th node
        of that type in *nodes*).
    config : RGATConfig
        Pipeline configuration (mutated in-place to set derived dims).

    Returns
    -------
    data : HeteroData
        Fully populated heterogeneous graph.
    node_index : NodeIndex
        Mapping ``{node_type: {node_id: int_idx}}``.
    """
    data = HeteroData()

    # ── 1. Node indexing ───────────────────────────────────────────────
    node_index: NodeIndex = {}
    nodes_by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for node in nodes:
        ntype = node["type"]
        nodes_by_type[ntype].append(node)

    for ntype, nlist in nodes_by_type.items():
        node_index[ntype] = {n["id"]: i for i, n in enumerate(nlist)}

    # ── 2. Feature tensors per node type ───────────────────────────────
    for ntype, nlist in nodes_by_type.items():
        scalar_keys = REQUIRED_SCALAR_FEATURES[ntype]
        n_nodes = len(nlist)
        n_scalar = len(scalar_keys)

        # Scalar features → float tensor
        scalar_tensor = torch.zeros(n_nodes, n_scalar, dtype=torch.float32)
        for i, node in enumerate(nlist):
            feats = node["features"]
            for j, key in enumerate(scalar_keys):
                val = feats[key]
                scalar_tensor[i, j] = float(val)

        # Leiden community IDs → long tensor (remap -1 → 0, others → id + 1)
        leiden_ids = torch.zeros(n_nodes, dtype=torch.long)
        for i, node in enumerate(nlist):
            lc = node["features"]["leiden_community"]
            leiden_ids[i] = 0 if lc == -1 else (lc + 1)

        # Sentence embeddings
        sent_emb = text_embeddings[ntype]  # [n_nodes, sentence_dim]
        assert sent_emb.shape[0] == n_nodes, (
            f"Text embedding count mismatch for {ntype}: "
            f"expected {n_nodes}, got {sent_emb.shape[0]}"
        )

        # Store on HeteroData
        data[ntype].x_scalar = scalar_tensor
        data[ntype].x_text = sent_emb
        data[ntype].leiden_ids = leiden_ids
        data[ntype].num_nodes = n_nodes

        # Record dimensions in config
        config.scalar_dims[ntype] = n_scalar
        config.input_dims[ntype] = n_scalar + config.sentence_dim + config.leiden_embed_dim

    # Update number of leiden community IDs in config
    all_leiden = torch.cat([data[nt].leiden_ids for nt in nodes_by_type])
    config.num_leiden_ids = int(all_leiden.max().item()) + 1

    # ── 3. Edge grouping by (src_type, relation, dst_type) ─────────────
    # Build a flat node-id → type lookup
    nid_to_type: Dict[str, str] = {}
    for node in nodes:
        nid_to_type[node["id"]] = node["type"]

    # Group edges
    edge_groups: Dict[Tuple[str, str, str], List[Tuple[int, int]]] = defaultdict(list)
    for edge in edges:
        src_id = edge["source"]
        dst_id = edge["target"]
        rel = edge["type"]
        src_type = nid_to_type[src_id]
        dst_type = nid_to_type[dst_id]
        triplet = (src_type, rel, dst_type)
        src_idx = node_index[src_type][src_id]
        dst_idx = node_index[dst_type][dst_id]
        edge_groups[triplet].append((src_idx, dst_idx))

    # Write edge_index tensors
    for triplet, pairs in edge_groups.items():
        src_type, rel, dst_type = triplet
        src_indices = [p[0] for p in pairs]
        dst_indices = [p[1] for p in pairs]
        edge_index = torch.tensor([src_indices, dst_indices], dtype=torch.long)
        data[src_type, rel, dst_type].edge_index = edge_index

    # ── 4. Reverse edges for message passing ───────────────────────────
    # Skip reverse for purely-structural hierarchy edges where the
    # reverse direction carries no semantic value (e.g. CONTAINS_MODULE).
    _SKIP_REVERSE = {"CONTAINS_MODULE", "CONTAINS_FILE", "IMPLEMENTS_MODULE"}
    reverse_groups: Dict[Tuple[str, str, str], Tensor] = {}
    for triplet, pairs in edge_groups.items():
        src_type, rel, dst_type = triplet
        if rel in _SKIP_REVERSE:
            continue
        rev_rel = f"rev_{rel}"
        rev_triplet = (dst_type, rev_rel, src_type)
        src_indices = [p[0] for p in pairs]
        dst_indices = [p[1] for p in pairs]
        # Reverse: swap src ↔ dst
        rev_edge_index = torch.tensor([dst_indices, src_indices], dtype=torch.long)
        data[dst_type, rev_rel, src_type].edge_index = rev_edge_index
        reverse_groups[rev_triplet] = rev_edge_index

    # ── 5. Same-repo labels (for auxiliary supervision) ────────────────
    # Node IDs follow format: type::repo_name::module_path::entity_name
    # Extract repo name from second segment and produce binary labels.
    def _repo_of(node_id: str) -> str:
        parts = node_id.split("::")
        return parts[1] if len(parts) >= 2 else ""

    # Build int_idx -> repo_name mapping per type
    idx_to_repo: Dict[str, Dict[int, str]] = {}
    for ntype, nlist in nodes_by_type.items():
        idx_to_repo[ntype] = {}
        for node in nlist:
            idx = node_index[ntype][node["id"]]
            idx_to_repo[ntype][idx] = _repo_of(node["id"])

    # For each edge triplet (original + reverse), compute same-repo labels
    all_triplets = list(edge_groups.keys()) + list(reverse_groups.keys())
    for triplet in all_triplets:
        src_type, rel, dst_type = triplet
        ei = data[triplet].edge_index
        n_edges = ei.shape[1]
        same_repo = torch.zeros(n_edges, dtype=torch.float32)
        src_repos = idx_to_repo.get(src_type, {})
        dst_repos = idx_to_repo.get(dst_type, {})
        for e in range(n_edges):
            s = int(ei[0, e])
            d = int(ei[1, e])
            if src_repos.get(s, "") == dst_repos.get(d, "") and src_repos.get(s, "") != "":
                same_repo[e] = 1.0
        data[triplet].same_repo_label = same_repo

    # ── 6. Degree buckets (for auxiliary supervision) ──────────────────
    # Discretise in-degree into log-scale buckets for node types that
    # have in_degree as a scalar feature.
    _DEGREE_FEATURE = "in_degree"
    num_buckets = 6
    for ntype, nlist in nodes_by_type.items():
        scalar_keys = REQUIRED_SCALAR_FEATURES[ntype]
        if _DEGREE_FEATURE not in scalar_keys:
            continue
        n_nodes = len(nlist)
        buckets = torch.zeros(n_nodes, dtype=torch.long)
        for i, node in enumerate(nlist):
            deg = float(node["features"].get(_DEGREE_FEATURE, 0))
            bucket = min(int(math.log2(deg + 1)), num_buckets - 1)
            buckets[i] = bucket
        data[ntype].degree_bucket = buckets

    # ── 7. Print dataset summary ───────────────────────────────────────
    _print_summary(data, nodes_by_type, edge_groups, reverse_groups, config)

    return data, node_index


def _print_summary(
    data: HeteroData,
    nodes_by_type: Dict[str, list],
    edge_groups: Dict[Tuple[str, str, str], list],
    reverse_groups: Dict[Tuple[str, str, str], Tensor],
    config: RGATConfig,
) -> None:
    """Print a human-readable dataset summary."""
    print("\n" + "=" * 65)
    print("  HETERO GRAPH SUMMARY")
    print("=" * 65)

    # Node counts
    print("\n  Node counts by type:")
    total_nodes = 0
    for ntype in sorted(nodes_by_type):
        n = len(nodes_by_type[ntype])
        total_nodes += n
        scalar_dim = config.scalar_dims.get(ntype, 0)
        total_dim = config.input_dims.get(ntype, 0)
        print(
            f"    {ntype:>10s}: {n:>7,} nodes  |  "
            f"scalar={scalar_dim}  text={config.sentence_dim}  "
            f"leiden_emb={config.leiden_embed_dim}  → total_input={total_dim}"
        )
    print(f"    {'TOTAL':>10s}: {total_nodes:>7,}")

    # Edge counts (original)
    print("\n  Edge counts by (src_type, relation, dst_type):")
    total_edges = 0
    for triplet in sorted(edge_groups):
        n = len(edge_groups[triplet])
        total_edges += n
        print(f"    {_fmt_triplet(triplet):>55s}: {n:>7,}")
    print(f"    {'TOTAL':>55s}: {total_edges:>7,}")

    # Reverse edge counts
    print("\n  Reverse edges added:")
    for triplet in sorted(reverse_groups):
        n = reverse_groups[triplet].shape[1]
        print(f"    {_fmt_triplet(triplet):>55s}: {n:>7,}")

    # Leiden community stats
    print(f"\n  Leiden community IDs: {config.num_leiden_ids:,} "
          f"(including isolate bucket)")
    print("=" * 65 + "\n")


def _fmt_triplet(triplet: Tuple[str, str, str]) -> str:
    return f"({triplet[0]}, {triplet[1]}, {triplet[2]})"
