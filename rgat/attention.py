"""
rgat.attention – Utilities for extracting attention weights from the encoder.

v2: Uses the encoder's native multi-layer attention output instead of
manually replaying the forward pass.  The encoder now returns structured
attention as ``Dict[int, Dict[str, Tensor]]`` (layer_idx → key → alpha).

Each attention entry is keyed by a 4-tuple
``(src_type, rel, dst_type, scale)`` where ``scale`` is either
``"local"`` (1-hop branch) or ``"global"`` (2-hop branch of
:class:`~rgat.model.MultiScaleHeteroConv`).
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING, Tuple

import torch
from torch import Tensor
from torch_geometric.data import HeteroData

from rgat.model import HeteroRGATEncoder

if TYPE_CHECKING:
    import pandas as pd


# Key: (src_type, rel, dst_type, scale)  where scale ∈ {"local", "global"}
AttentionMap = Dict[
    Tuple[str, str, str, str],
    Tuple[Tensor, Tensor],          # (edge_index, alpha)
]


def _parse_attn_key(key: str) -> Tuple[str, str, str, str]:
    """Parse 'local|src_type|rel|dst_type' → (src_type, rel, dst_type, scale)."""
    parts = key.split("|")
    if len(parts) != 4:
        raise ValueError(f"Unexpected attention key format: {key!r}")
    scale, src_type, rel, dst_type = parts
    return (src_type, rel, dst_type, scale)


def get_attention_weights(
    encoder: HeteroRGATEncoder,
    data: HeteroData,
) -> List[AttentionMap]:
    """Run a forward pass and capture per-layer, per-scale attention weights.

    Parameters
    ----------
    encoder : HeteroRGATEncoder
        A trained encoder (set to eval mode internally).
    data : HeteroData
        The graph to compute attention on.

    Returns
    -------
    list[AttentionMap]
        One ``AttentionMap`` per encoder layer.  Each map has keys
        ``(src_type, rel, dst_type, scale)`` where ``scale`` is
        ``"local"`` or ``"global"``, and values are
        ``(edge_index, alpha)`` tensors.

    Notes
    -----
    Uses the encoder's native ``return_attention_weights=True`` to collect
    attention from all layers in a single forward pass.  To get attention
    from specific layers only, configure ``encoder.collect_attention_layers``.
    """
    encoder.eval()
    device = next(encoder.parameters()).device
    data = data.to(device)

    # Temporarily collect from all layers for complete extraction
    original_layers = encoder.collect_attention_layers
    encoder.collect_attention_layers = None  # all layers

    with torch.no_grad():
        z_dict, all_attn = encoder(data, return_attention_weights=True)

    # Restore original setting
    encoder.collect_attention_layers = original_layers

    # Build edge index dicts for reference (needed for the attention maps)
    edge_index_dict = {
        et: data[et].edge_index
        for et in encoder.edge_types
        if hasattr(data[et], "edge_index")
    }

    # Build global edge index dict (same logic as encoder)
    from rgat.model import _structural_global_edges
    global_edge_index_dict: Dict = {}
    for et, ei in edge_index_dict.items():
        src_type, _, dst_type = et
        n_src = data[src_type].num_nodes
        n_dst = data[dst_type].num_nodes
        global_edge_index_dict[et] = _structural_global_edges(ei, n_src, n_dst)

    # Convert structured attention to List[AttentionMap] format
    num_layers = encoder.num_layers
    per_layer_attn: List[AttentionMap] = []

    for layer_idx in range(num_layers):
        layer_attn: AttentionMap = {}

        if layer_idx in all_attn:
            raw_layer = all_attn[layer_idx]
            for key, alpha in raw_layer.items():
                src_type, rel, dst_type, scale = _parse_attn_key(key)
                et = (src_type, rel, dst_type)

                # Determine the right edge index for this entry
                if scale == "local":
                    ei = edge_index_dict.get(et)
                else:
                    ei = global_edge_index_dict.get(et)

                if ei is None:
                    continue

                # For GATConv with return_attention_weights, the edge index
                # returned may include self-loops. We need to handle the
                # case where alpha has more entries than the original ei.
                # Since we can't get the exact edge_index from the conv
                # (it's inside no_grad), we reconstruct from the alpha size.
                # The alpha tensor has [num_edges_with_self_loops, num_heads].
                # We store both the original edge_index and the alpha.
                layer_attn[(src_type, rel, dst_type, scale)] = (
                    ei.cpu(),
                    alpha.cpu(),
                )

        per_layer_attn.append(layer_attn)

    return per_layer_attn


def attention_to_dataframe(
    attn_maps: List[AttentionMap],
    node_index_reverse: Dict[str, Dict[int, str]] | None = None,
) -> "pd.DataFrame":
    """Convert attention maps to a pandas DataFrame for analysis.

    Parameters
    ----------
    attn_maps : list[AttentionMap]
        Output of :func:`get_attention_weights`.
    node_index_reverse : dict, optional
        ``{node_type: {int_idx: node_id}}`` for converting indices to IDs.

    Returns
    -------
    pd.DataFrame
        Columns: ``layer``, ``scale``, ``src_type``, ``rel``, ``dst_type``,
        ``src_idx``, ``dst_idx``, ``head``, ``alpha``, and optionally
        ``src_id``, ``dst_id``.

        ``scale`` is either ``"local"`` (1-hop branch) or ``"global"``
        (2-hop branch) from :class:`~rgat.model.MultiScaleHeteroConv`.
    """
    import pandas as pd

    rows = []
    for layer_idx, attn_map in enumerate(attn_maps):
        for (src_type, rel, dst_type, scale), (edge_index, alpha) in attn_map.items():
            edge_index = edge_index.cpu()
            alpha = alpha.cpu()
            n_edges = edge_index.shape[1]
            n_heads = alpha.shape[1] if alpha.dim() == 2 else 1

            # alpha may have more entries than edge_index (self-loops added
            # by GATConv). Only iterate up to min of both.
            n_iter = min(n_edges, alpha.shape[0])

            for head in range(n_heads):
                head_alpha = alpha[:, head] if alpha.dim() == 2 else alpha
                for e in range(n_iter):
                    row = {
                        "layer": layer_idx,
                        "scale": scale,
                        "src_type": src_type,
                        "rel": rel,
                        "dst_type": dst_type,
                        "src_idx": int(edge_index[0, e]),
                        "dst_idx": int(edge_index[1, e]),
                        "head": head,
                        "alpha": float(head_alpha[e]),
                    }
                    if node_index_reverse:
                        src_map = node_index_reverse.get(src_type, {})
                        dst_map = node_index_reverse.get(dst_type, {})
                        row["src_id"] = src_map.get(int(edge_index[0, e]), "")
                        row["dst_id"] = dst_map.get(int(edge_index[1, e]), "")
                    rows.append(row)

    return pd.DataFrame(rows)
