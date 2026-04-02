"""
rgat.edge_split – Train / validation edge masking via PyG RandomLinkSplit.

Splits supervised edge types into training and validation sets while
keeping containment / structural edges intact for message passing.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import torch
from torch_geometric.data import HeteroData
from torch_geometric.transforms import RandomLinkSplit

from rgat.config import RGATConfig


def split_edges(
    data: HeteroData,
    config: RGATConfig,
) -> Tuple[HeteroData, HeteroData]:
    """Split supervised edges into train and validation sets.

    Parameters
    ----------
    data : HeteroData
        Full heterogeneous graph (with both original and reverse edges).
    config : RGATConfig
        Pipeline configuration.

    Returns
    -------
    train_data, val_data : HeteroData
        The train split contains message-passing edges for **all** edge
        types, plus ``edge_label`` / ``edge_label_index`` attributes on
        the supervised edge types.  The val split has the held-out
        supervision edges.
    """
    # ── Identify supervised edge triplets from the data ────────────────
    supervised_triplets: List[Tuple[str, str, str]] = []
    rev_triplets: List[Tuple[str, str, str]] = []

    for triplet in data.edge_types:
        src_type, rel, dst_type = triplet
        if rel in config.supervised_relations:
            supervised_triplets.append(triplet)
            # Find the corresponding reverse edge
            rev_rel = f"rev_{rel}"
            rev_triplet = (dst_type, rev_rel, src_type)
            if rev_triplet in data.edge_types:
                rev_triplets.append(rev_triplet)
            else:
                rev_triplets.append(None)

    if not supervised_triplets:
        raise ValueError(
            f"No supervised edge triplets found in the graph for relations "
            f"{config.supervised_relations}. Available edge types: "
            f"{data.edge_types}"
        )

    print(f"[edge_split] Supervised triplets ({len(supervised_triplets)}):")
    for triplet, rev in zip(supervised_triplets, rev_triplets):
        n_edges = data[triplet].edge_index.shape[1]
        rev_str = f" ↔ {rev}" if rev else " (no reverse)"
        print(f"  {triplet}  ({n_edges:,} edges){rev_str}")

    # ── Apply RandomLinkSplit ──────────────────────────────────────────
    transform = RandomLinkSplit(
        num_val=config.val_ratio,
        num_test=0.0,
        is_undirected=False,
        edge_types=supervised_triplets,
        rev_edge_types=rev_triplets,
        add_negative_train_samples=True,
        neg_sampling_ratio=config.neg_sampling_ratio,
        disjoint_train_ratio=0.0,
        split_labels=False,
    )

    train_data, val_data, _ = transform(data)

    # ── Print split summary ────────────────────────────────────────────
    print("\n[edge_split] Edge split summary:")
    print(f"  {'Triplet':<55s} {'Train+':>8s} {'Train-':>8s} {'Val+':>8s} {'Val-':>8s}")
    print("  " + "-" * 85)

    for triplet in supervised_triplets:
        # Train
        train_labels = train_data[triplet].edge_label
        train_pos = int((train_labels == 1).sum())
        train_neg = int((train_labels == 0).sum())
        # Val
        val_labels = val_data[triplet].edge_label
        val_pos = int((val_labels == 1).sum())
        val_neg = int((val_labels == 0).sum())

        triplet_str = f"({triplet[0]}, {triplet[1]}, {triplet[2]})"
        print(
            f"  {triplet_str:<55s} "
            f"{train_pos:>8,} {train_neg:>8,} "
            f"{val_pos:>8,} {val_neg:>8,}"
        )

    print()
    return train_data, val_data
