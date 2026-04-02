"""
rgat.attention – Utilities for extracting attention weights from the encoder.

After training, attention weights reveal which edges (relationships) the
model considers most important.  This module provides hook-based capture
so you can inspect per-layer, per-edge-type, per-scale attention
coefficients without modifying the encoder's ``forward()`` method.

Each attention entry is keyed by a 4-tuple
``(src_type, rel, dst_type, scale)`` where ``scale`` is either
``"local"`` (1-hop branch) or ``"global"`` (2-hop branch of
:class:`~rgat.model.MultiScaleHeteroConv`).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, TYPE_CHECKING, Tuple

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
    Performs a manual two-scale extraction that mirrors
    :class:`~rgat.model.MultiScaleHeteroConv` forward logic: each
    sub-conv (local branch, then global branch) is called individually
    with ``return_attention_weights=True`` so heads within each scale
    can be inspected independently.
    """
    import torch.nn.functional as F

    encoder.eval()
    device = next(encoder.parameters()).device
    data = data.to(device)

    from rgat.model import _structural_global_edges

    # ── Reproduce input projection (same as encoder.forward) ──────────
    with torch.no_grad():
        x_dict: Dict[str, Tensor] = {}
        for ntype in encoder.node_types:
            x_scalar = data[ntype].x_scalar
            x_text = data[ntype].x_text
            leiden_ids = data[ntype].leiden_ids
            x_leiden = encoder.leiden_embedding(leiden_ids)
            x_cat = torch.cat([x_scalar, x_text, x_leiden], dim=-1)
            x_dict[ntype] = F.elu(encoder.input_proj[ntype](x_cat))

        edge_index_dict = {
            et: data[et].edge_index
            for et in encoder.edge_types
            if hasattr(data[et], "edge_index")
        }

        # Build the same global edge index the encoder uses at runtime
        global_edge_index_dict: Dict = {}
        for et, ei in edge_index_dict.items():
            src_type, _, dst_type = et
            n_src = data[src_type].num_nodes
            n_dst = data[dst_type].num_nodes
            global_edge_index_dict[et] = _structural_global_edges(ei, n_src, n_dst)

        per_layer_attn: List[AttentionMap] = []

        for layer_idx, (ms_conv, norm_dict) in enumerate(
            zip(encoder.convs, encoder.norms)
        ):
            layer_attn: AttentionMap = {}
            x_residual = x_dict

            # ── LOCAL PASS: 1-hop sub-convs ────────────────────────────
            local_out_dict: Dict[str, List[Tensor]] = {}
            for et in ms_conv.edge_types:
                key = ms_conv._et_keys[et]
                src_type, rel, dst_type = et
                if et not in edge_index_dict:
                    continue
                ei = edge_index_dict[et]
                sub_conv = ms_conv.local_convs[key]
                result = sub_conv(
                    (x_dict[src_type], x_dict[dst_type]), ei,
                    return_attention_weights=True,
                )
                if isinstance(result, tuple) and len(result) == 2:
                    out_feat, attn_info = result
                    if isinstance(attn_info, tuple) and len(attn_info) == 2:
                        layer_attn[(src_type, rel, dst_type, "local")] = (
                            attn_info[0].cpu(),
                            attn_info[1].cpu(),
                        )
                else:
                    out_feat = result
                local_out_dict.setdefault(dst_type, []).append(out_feat)

            # Assemble h_local (sum contributions, identity for unseen types)
            h_local: Dict[str, Tensor] = {}
            for ntype in encoder.node_types:
                parts = local_out_dict.get(ntype, [])
                if parts:
                    h_local[ntype] = torch.stack(parts, dim=0).sum(dim=0)
                else:
                    h_local[ntype] = x_dict[ntype]

            # ── GLOBAL PASS: augmented sub-convs (operate on x_dict) ───
            global_out_dict: Dict[str, List[Tensor]] = {}
            for et in ms_conv.edge_types:
                key = ms_conv._et_keys[et]
                src_type, rel, dst_type = et
                if et not in global_edge_index_dict:
                    continue
                ei = global_edge_index_dict[et]
                sub_conv = ms_conv.global_convs[key]
                result = sub_conv(
                    (x_dict[src_type], x_dict[dst_type]), ei,
                    return_attention_weights=True,
                )
                if isinstance(result, tuple) and len(result) == 2:
                    out_feat, attn_info = result
                    if isinstance(attn_info, tuple) and len(attn_info) == 2:
                        layer_attn[(src_type, rel, dst_type, "global")] = (
                            attn_info[0].cpu(),
                            attn_info[1].cpu(),
                        )
                else:
                    out_feat = result
                global_out_dict.setdefault(dst_type, []).append(out_feat)

            per_layer_attn.append(layer_attn)

            # ── Combine + norm + residual (mirrors MultiScaleHeteroConv) ─
            is_last = (layer_idx == len(encoder.convs) - 1)
            new_x: Dict[str, Tensor] = {}
            for ntype in encoder.node_types:
                loc = h_local.get(ntype, x_dict[ntype])
                glb_parts = global_out_dict.get(ntype, [])
                glb = (
                    torch.stack(glb_parts, dim=0).sum(dim=0)
                    if glb_parts else loc
                )
                h = ms_conv.scale_combine[ntype](torch.cat([loc, glb], dim=-1))
                h = norm_dict[ntype](h)
                h = h + x_residual[ntype]
                if not is_last:
                    h = F.elu(h)
                new_x[ntype] = h
            x_dict = new_x

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

            for head in range(n_heads):
                head_alpha = alpha[:, head] if alpha.dim() == 2 else alpha
                for e in range(n_edges):
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
