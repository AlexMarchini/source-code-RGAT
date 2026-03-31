"""
rgat.model -- Heterogeneous RGAT encoder and link-prediction decoder.

Architecture
------------
1. Per-type input projection: (scalar || sentence || leiden) -> hidden_dim
2. Stacked MultiScaleHeteroConv layers using GATConv (v1) with:
   - Local branch (num_heads//2): 1-hop attention on direct neighbours
   - Global branch (remaining heads): structural-role augmented edges
   - Self-loops on same-type edges only
   - Per-head diverse initialisation of att_src / att_dst
   - Per-type scale_combine: [local || global] -> hidden_dim
3. LayerNorm + residual + ELU + dropout between layers
4. L2-normalised output embeddings
5. Dot-product LinkPredictor
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from torch_geometric.data import HeteroData
from torch_geometric.nn import GATConv


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _diversify_gat_heads(conv: GATConv) -> None:
    """Break head-parameter symmetry for GATConv with separate att_src/att_dst.

    Cycles through three init strategies per head to ensure strong asymmetry
    from the start.
    """
    if not hasattr(conv, "att_src") or not hasattr(conv, "att_dst"):
        return
    H = conv.att_src.shape[1]       # number of heads
    C = conv.att_src.shape[2]       # out_channels per head
    with torch.no_grad():
        for h in range(H):
            if h % 3 == 0:
                nn.init.xavier_uniform_(conv.att_src.data[:, h:h + 1])
                nn.init.xavier_uniform_(conv.att_dst.data[:, h:h + 1])
                conv.att_src.data[:, h] += torch.randn_like(conv.att_src.data[:, h]) * 0.1
                conv.att_dst.data[:, h] += torch.randn_like(conv.att_dst.data[:, h]) * 0.1
                for lin in (conv.lin_src, conv.lin_dst):
                    if lin is not None and hasattr(lin, "weight"):
                        nn.init.xavier_uniform_(lin.weight.data[h * C:(h + 1) * C])
                        lin.weight.data[h * C:(h + 1) * C] += (
                            torch.randn_like(lin.weight.data[h * C:(h + 1) * C]) * 0.1
                        )
            elif h % 3 == 1:
                scale = 0.2 + h * 0.1
                nn.init.uniform_(conv.att_src.data[:, h:h + 1], -scale, scale)
                nn.init.uniform_(conv.att_dst.data[:, h:h + 1], -scale, scale)
                for lin in (conv.lin_src, conv.lin_dst):
                    if lin is not None and hasattr(lin, "weight"):
                        nn.init.uniform_(lin.weight.data[h * C:(h + 1) * C], -scale, scale)
            else:
                std = 0.2 + h * 0.05
                nn.init.normal_(conv.att_src.data[:, h:h + 1], mean=0, std=std)
                nn.init.normal_(conv.att_dst.data[:, h:h + 1], mean=0, std=std)
                for lin in (conv.lin_src, conv.lin_dst):
                    if lin is not None and hasattr(lin, "weight"):
                        nn.init.normal_(lin.weight.data[h * C:(h + 1) * C], mean=0, std=std)


def _structural_global_edges(
    edge_index: Tensor,
    n_src: int,
    n_dst: int,
    max_per_node: int = 6,
    walk_len: int = 3,
    rng_seed: int = 0,
) -> Tensor:
    """Augment a homogeneous edge index with structural-role edges.

    For heterogeneous edge types (n_src != n_dst) the original index is
    returned unchanged.
    """
    if n_src != n_dst or edge_index.size(1) == 0:
        return edge_index

    rng = random.Random(rng_seed)
    N = n_src
    adj: Dict[int, List[int]] = {i: [] for i in range(N)}
    existing: Set[Tuple[int, int]] = set()
    in_deg = [0] * N

    for i in range(edge_index.size(1)):
        a = int(edge_index[0, i])
        b = int(edge_index[1, i])
        adj[a].append(b)
        existing.add((a, b))
        in_deg[b] += 1

    out_deg = [len(adj[i]) for i in range(N)]
    added: Dict[int, int] = {i: 0 for i in range(N)}

    new_src: List[int] = []
    new_dst: List[int] = []

    def _add(a: int, c: int) -> bool:
        if c != a and (a, c) not in existing and added[a] < max_per_node:
            new_src.append(a)
            new_dst.append(c)
            existing.add((a, c))
            added[a] += 1
            return True
        return False

    # Degree-bucket edges
    buckets: Dict[int, List[int]] = {}
    for node in range(N):
        b = int(math.log2(out_deg[node] + 1))
        buckets.setdefault(b, []).append(node)

    for bucket_nodes in buckets.values():
        if len(bucket_nodes) < 2:
            continue
        shuffled = bucket_nodes[:]
        rng.shuffle(shuffled)
        for i in range(0, len(shuffled) - 1, 2):
            a, c = shuffled[i], shuffled[i + 1]
            _add(a, c)
            _add(c, a)

    # Random-walk co-occurrence edges
    for start in range(N):
        if not adj[start]:
            continue
        cur = start
        visited: List[int] = []
        for _ in range(walk_len):
            if not adj[cur]:
                break
            cur = rng.choice(adj[cur])
            if cur != start:
                visited.append(cur)
        for v in visited:
            if (start, v) not in existing:
                _add(start, v)

    if not new_src:
        return edge_index

    extra = torch.tensor(
        [new_src, new_dst], dtype=edge_index.dtype, device=edge_index.device
    )
    return torch.cat([edge_index, extra], dim=1)


# ------------------------------------------------------------------ #
# Multi-scale heterogeneous convolution block                          #
# ------------------------------------------------------------------ #

class MultiScaleHeteroConv(nn.Module):
    """Two-scale GATConv (v1) with separate att_src / att_dst per head.

    Manually iterates edge types (instead of HeteroConv) so that
    ``return_attention_weights`` can be forwarded to each sub-conv.
    Self-loops are enabled only for same-type edges.
    """

    def __init__(
        self,
        edge_types: List[Tuple[str, str, str]],
        node_types: List[str],
        hidden_dim: int,
        num_heads: int,
        dropout: float,
        lrelu_slope: float = 0.2,
    ) -> None:
        super().__init__()

        local_heads = num_heads // 2
        global_heads = num_heads - local_heads
        local_head_dim = hidden_dim // local_heads
        global_head_dim = hidden_dim // global_heads
        global_slope = lrelu_slope * 1.5

        self.local_convs = nn.ModuleDict()
        self.global_convs = nn.ModuleDict()
        self._et_keys: Dict[Tuple[str, str, str], str] = {}

        for et in edge_types:
            src_type, rel, dst_type = et
            key = f"{src_type}__{rel}__{dst_type}"
            self._et_keys[et] = key
            is_homo = (src_type == dst_type)

            self.local_convs[key] = GATConv(
                in_channels=(hidden_dim, hidden_dim),
                out_channels=local_head_dim,
                heads=local_heads,
                concat=True,
                dropout=dropout,
                add_self_loops=is_homo,
                negative_slope=lrelu_slope,
            )
            self.global_convs[key] = GATConv(
                in_channels=(hidden_dim, hidden_dim),
                out_channels=global_head_dim,
                heads=global_heads,
                concat=True,
                dropout=dropout,
                add_self_loops=is_homo,
                negative_slope=global_slope,
            )

        self.scale_combine = nn.ModuleDict({
            ntype: nn.Linear(2 * hidden_dim, hidden_dim, bias=False)
            for ntype in node_types
        })

        self.edge_types = edge_types
        self.node_types = node_types

        # Per-head diverse init
        for convs_dict in (self.local_convs, self.global_convs):
            for sub_conv in convs_dict.values():
                _diversify_gat_heads(sub_conv)

    def forward(
        self,
        x_dict: Dict[str, Tensor],
        edge_index_dict: Dict,
        global_edge_index_dict: Optional[Dict] = None,
        return_attention_weights: bool = False,
    ):
        if global_edge_index_dict is None:
            global_edge_index_dict = edge_index_dict

        local_out: Dict[str, List[Tensor]] = defaultdict(list)
        global_out: Dict[str, List[Tensor]] = defaultdict(list)
        attn_dict: Dict[str, Tensor] = {}

        for et in self.edge_types:
            key = self._et_keys[et]
            src_type, rel, dst_type = et
            if et not in edge_index_dict:
                continue

            x_src = x_dict[src_type]
            x_dst = x_dict[dst_type]
            ei = edge_index_dict[et]

            # ---- Local conv ----
            if return_attention_weights:
                out_l, (_, alpha_l) = self.local_convs[key](
                    (x_src, x_dst), ei, return_attention_weights=True,
                )
                attn_dict[f"local|{src_type}|{rel}|{dst_type}"] = alpha_l
            else:
                out_l = self.local_convs[key]((x_src, x_dst), ei)
            local_out[dst_type].append(out_l)

            # ---- Global conv ----
            g_ei = global_edge_index_dict.get(et, ei)
            if return_attention_weights:
                out_g, (_, alpha_g) = self.global_convs[key](
                    (x_src, x_dst), g_ei, return_attention_weights=True,
                )
                attn_dict[f"global|{src_type}|{rel}|{dst_type}"] = alpha_g
            else:
                out_g = self.global_convs[key]((x_src, x_dst), g_ei)
            global_out[dst_type].append(out_g)

        # ---- Aggregate and combine scales ----
        result: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            if ntype in local_out and local_out[ntype]:
                loc = torch.stack(local_out[ntype], dim=0).sum(dim=0)
                glb = (
                    torch.stack(global_out[ntype], dim=0).sum(dim=0)
                    if ntype in global_out and global_out[ntype]
                    else x_dict[ntype]
                )
                result[ntype] = self.scale_combine[ntype](
                    torch.cat([loc, glb], dim=-1)
                )
            else:
                result[ntype] = x_dict[ntype]

        if return_attention_weights:
            return result, attn_dict
        return result


# ------------------------------------------------------------------ #
# Heterogeneous RGAT Encoder                                           #
# ------------------------------------------------------------------ #

class HeteroRGATEncoder(nn.Module):
    """Multi-layer heterogeneous encoder with multi-scale GATConv attention.

    Uses GATConv (v1) with separate att_src / att_dst for better head
    diversification.  Supports ``return_attention_weights`` for gradient-
    connected diversity loss during training.
    """

    def __init__(
        self,
        node_types: List[str],
        edge_types: List[Tuple[str, str, str]],
        scalar_dims: Dict[str, int],
        sentence_dim: int = 384,
        leiden_embed_dim: int = 16,
        num_leiden_ids: int = 2875,
        hidden_dim: int = 128,
        num_heads: int = 4,
        num_layers: int = 2,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.node_types = node_types
        self.edge_types = edge_types
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.dropout = dropout

        self.leiden_embedding = nn.Embedding(
            num_leiden_ids, leiden_embed_dim, padding_idx=0,
        )

        self.input_proj = nn.ModuleDict()
        for ntype in node_types:
            in_dim = scalar_dims[ntype] + sentence_dim + leiden_embed_dim
            self.input_proj[ntype] = nn.Linear(in_dim, hidden_dim)

        self.convs = nn.ModuleList()
        self.norms = nn.ModuleList()
        for _ in range(num_layers):
            self.convs.append(MultiScaleHeteroConv(
                edge_types=edge_types,
                node_types=node_types,
                hidden_dim=hidden_dim,
                num_heads=num_heads,
                dropout=dropout,
            ))
            self.norms.append(nn.ModuleDict({
                ntype: nn.LayerNorm(hidden_dim) for ntype in node_types
            }))

    def forward(
        self, data: HeteroData, return_attention_weights: bool = False,
    ):
        # ---- Input projection ----
        x_dict: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            x_cat = torch.cat([
                data[ntype].x_scalar,
                data[ntype].x_text,
                self.leiden_embedding(data[ntype].leiden_ids),
            ], dim=-1)
            x_dict[ntype] = self.input_proj[ntype](x_cat)

        x_dict = {k: F.elu(v) for k, v in x_dict.items()}

        # ---- Edge indices ----
        edge_index_dict = {
            et: data[et].edge_index
            for et in self.edge_types
            if hasattr(data[et], "edge_index")
        }

        # ---- Structural-role global edges (cached per data object) ----
        data_id = id(data)
        if not hasattr(self, "_global_ei_cache") or self._global_ei_cache_id != data_id:
            global_ei: Dict = {}
            for et, ei in edge_index_dict.items():
                src_type, _, dst_type = et
                global_ei[et] = _structural_global_edges(
                    ei, data[src_type].num_nodes, data[dst_type].num_nodes,
                )
            self._global_ei_cache = global_ei
            self._global_ei_cache_id = data_id
        else:
            global_ei = self._global_ei_cache

        all_attn: Dict[str, Tensor] = {}

        for i, (conv, norm_dict) in enumerate(zip(self.convs, self.norms)):
            x_residual = x_dict

            # Only collect attention from the first layer
            if return_attention_weights and i == 0:
                x_dict, layer_attn = conv(
                    x_dict, edge_index_dict, global_ei,
                    return_attention_weights=True,
                )
                all_attn = layer_attn
            else:
                x_dict = conv(x_dict, edge_index_dict, global_ei)

            # LayerNorm + residual + activation + dropout
            is_last = (i == len(self.convs) - 1)
            new_x: Dict[str, Tensor] = {}
            for ntype in self.node_types:
                if ntype in x_dict:
                    h = norm_dict[ntype](x_dict[ntype])
                    h = h + x_residual[ntype]
                    if not is_last:
                        h = F.elu(h)
                    h = F.dropout(h, p=self.dropout, training=self.training)
                    new_x[ntype] = h
                else:
                    new_x[ntype] = x_residual[ntype]
            x_dict = new_x

        x_dict = {k: F.normalize(v, p=2, dim=-1) for k, v in x_dict.items()}

        if return_attention_weights:
            return x_dict, all_attn
        return x_dict


# ------------------------------------------------------------------ #
# Attention Diversity Loss                                             #
# ------------------------------------------------------------------ #

class AttentionDiversityLoss(nn.Module):
    """3-term diversity loss on actual attention outputs.

    Must be called with gradient-connected attention tensors (not under
    torch.no_grad) for the loss to influence training.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cached_attn: Dict[str, Tensor] = {}

    @staticmethod
    def _gini(x: Tensor) -> Tensor:
        sorted_x, _ = torch.sort(x)
        n = sorted_x.size(0)
        idx = torch.arange(1, n + 1, dtype=torch.float, device=x.device)
        total = sorted_x.sum()
        if total < 1e-12:
            return torch.tensor(0.0, device=x.device)
        gini = (2.0 * (idx * sorted_x).sum()) / (n * total) - (n + 1.0) / n
        return torch.clamp(gini, 0.0, 1.0)

    def forward(self, encoder: HeteroRGATEncoder) -> Tensor:
        if self.cached_attn:
            return self._output_based_loss()
        return self._param_level_loss(encoder)

    def _output_based_loss(self) -> Tensor:
        device = next(iter(self.cached_attn.values())).device
        total_loss = torch.tensor(0.0, device=device)
        n_types = 0

        for _key, attn in self.cached_attn.items():
            if attn.dim() != 2 or attn.size(1) < 2 or attn.size(0) < 2:
                continue

            H = attn.size(1)
            heads = attn.T  # [H, num_edges]

            # 1. Entropy loss
            entropy_loss = torch.tensor(0.0, device=device)
            for h in range(H):
                p = F.softmax(heads[h], dim=0)
                ent = -(p * torch.log(p + 1e-8)).sum()
                max_ent = torch.log(torch.tensor(float(heads.size(1)), device=device))
                entropy_loss = entropy_loss + (1.0 - ent / max_ent)

            # 2. Head orthogonality
            heads_norm = F.normalize(heads, p=2, dim=1, eps=1e-8)
            sim = torch.mm(heads_norm, heads_norm.t())
            mask = ~torch.eye(H, dtype=torch.bool, device=device)
            ortho_loss = (
                sim[mask].abs().mean()
                if mask.any()
                else torch.tensor(0.0, device=device)
            )

            # 3. Gini sparsity (alternate per head)
            gini_loss = torch.tensor(0.0, device=device)
            for h in range(H):
                p = F.softmax(heads[h], dim=0)
                g = self._gini(p)
                if h % 2 == 0:
                    gini_loss = gini_loss + (1.0 - g)
                else:
                    gini_loss = gini_loss + g

            edge_loss = 0.3 * entropy_loss + 0.4 * ortho_loss + 0.3 * gini_loss
            if torch.isfinite(edge_loss):
                total_loss = total_loss + edge_loss
                n_types += 1

        if n_types > 0:
            total_loss = total_loss / n_types
        if not torch.isfinite(total_loss):
            total_loss = torch.tensor(0.0, device=device)
        return total_loss

    def _param_level_loss(self, encoder: HeteroRGATEncoder) -> Tensor:
        terms: List[Tensor] = []
        for ms_conv in encoder.convs:
            for convs_dict in (ms_conv.local_convs, ms_conv.global_convs):
                for sub_conv in convs_dict.values():
                    if not hasattr(sub_conv, "att_src"):
                        continue
                    H = sub_conv.att_src.shape[1]
                    if H < 2:
                        continue
                    mask = ~torch.eye(H, dtype=torch.bool, device=sub_conv.att_src.device)

                    # att_src similarity
                    src_heads = sub_conv.att_src[0]
                    src_norm = F.normalize(src_heads, p=2, dim=1, eps=1e-8)
                    sim_src = torch.mm(src_norm, src_norm.t())
                    terms.append(sim_src[mask].abs().mean())

                    # att_dst similarity
                    dst_heads = sub_conv.att_dst[0]
                    dst_norm = F.normalize(dst_heads, p=2, dim=1, eps=1e-8)
                    sim_dst = torch.mm(dst_norm, dst_norm.t())
                    terms.append(sim_dst[mask].abs().mean())

                    # Linear weight similarity
                    for lin in (sub_conv.lin_src, sub_conv.lin_dst):
                        if lin is None or not hasattr(lin, "weight"):
                            continue
                        w = lin.weight
                        H_C = w.shape[0]
                        C = H_C // H
                        w_per_head = w.view(H, C * w.shape[1])
                        w_norm = F.normalize(w_per_head, p=2, dim=1, eps=1e-8)
                        sim_w = torch.mm(w_norm, w_norm.t())
                        terms.append(sim_w[mask].abs().mean())

        if not terms:
            device = next(encoder.parameters()).device
            return torch.tensor(0.0, device=device)
        return torch.stack(terms).mean()


# ------------------------------------------------------------------ #
# Link Predictor (dot-product decoder)                                 #
# ------------------------------------------------------------------ #

class LinkPredictor(nn.Module):
    """Dot-product link predictor."""

    def forward(self, z_src: Tensor, z_dst: Tensor) -> Tensor:
        return (z_src * z_dst).sum(dim=-1)
