"""
rgat.model -- Heterogeneous RGAT encoder and link-prediction decoder (v2).

Changes from v1
----------------
- Multi-layer attention collection (configurable which layers)
- Gated per-relation aggregation (replaces plain sum)
- Learned sigmoid gate for local/global branch combination
- Relation-specific decoder (DistMult or bilinear)
- Improved AttentionDiversityLoss (orthogonality + variance + coverage)
- Optional auxiliary heads (same-repo, degree-bucket)
- Structured attention output: Dict[int, Dict[str, Tensor]]

Architecture
------------
1. Per-type input projection: (scalar || sentence || leiden) -> hidden_dim
2. Stacked MultiScaleHeteroConv layers using GATConv (v1) with:
   - Local branch (num_heads//2): 1-hop attention on direct neighbours
   - Global branch (remaining heads): structural-role augmented edges
   - Self-loops on same-type edges only
   - Per-head diverse initialisation of att_src / att_dst
   - Learned per-relation gating before aggregation
   - Sigmoid gate for local/global combination
3. LayerNorm + residual + ELU + dropout between layers
4. L2-normalised output embeddings
5. Relation-specific decoder (DistMult default)
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
    """Two-scale GATConv (v1) with gated per-relation aggregation and
    learned local/global combination.

    Changes from v1:
    - Per-relation softmax gates (replaces plain sum over relations)
    - Sigmoid gate for local/global combination (replaces concat+linear)
    - Gate values stored for interpretability
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

        # ── Per-relation gating ──────────────────────────────────────
        # For each destination node type, learn a weight per incoming relation.
        # We build the mapping: dst_type -> list of relation keys that target it.
        self._dst_to_rels: Dict[str, List[str]] = defaultdict(list)
        for et in edge_types:
            src_type, rel, dst_type = et
            key = self._et_keys[et]
            self._dst_to_rels[dst_type].append(key)

        # Gate parameters: one learnable scalar per incoming relation per dst type
        self.relation_gates = nn.ParameterDict()
        for ntype in node_types:
            n_rels = len(self._dst_to_rels[ntype])
            if n_rels > 0:
                # Initialise uniformly so initial behaviour ≈ mean
                self.relation_gates[ntype] = nn.Parameter(
                    torch.zeros(n_rels)
                )

        # ── Learned local/global sigmoid gate ────────────────────────
        # gate = sigmoid(gate_proj(cat([local, global])))
        # combined = gate * local + (1 - gate) * global
        self.gate_proj = nn.ModuleDict({
            ntype: nn.Linear(2 * hidden_dim, hidden_dim)
            for ntype in node_types
        })
        self.output_proj = nn.ModuleDict({
            ntype: nn.Linear(hidden_dim, hidden_dim, bias=False)
            for ntype in node_types
        })

        self.edge_types = edge_types
        self.node_types = node_types

        # Store last gate values for interpretability
        self.last_branch_gate: Dict[str, Tensor] = {}

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

        # Collect per-relation outputs keyed by (dst_type, rel_key)
        local_per_rel: Dict[str, Dict[str, Tensor]] = defaultdict(dict)
        global_per_rel: Dict[str, Dict[str, Tensor]] = defaultdict(dict)
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
            local_per_rel[dst_type][key] = out_l

            # ---- Global conv ----
            g_ei = global_edge_index_dict.get(et, ei)
            if return_attention_weights:
                out_g, (_, alpha_g) = self.global_convs[key](
                    (x_src, x_dst), g_ei, return_attention_weights=True,
                )
                attn_dict[f"global|{src_type}|{rel}|{dst_type}"] = alpha_g
            else:
                out_g = self.global_convs[key]((x_src, x_dst), g_ei)
            global_per_rel[dst_type][key] = out_g

        # ---- Gated per-relation aggregation + local/global combine ----
        result: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            rel_keys = self._dst_to_rels[ntype]

            if not rel_keys or ntype not in local_per_rel:
                result[ntype] = x_dict[ntype]
                continue

            # Collect per-relation outputs that are actually present
            present_local = []
            present_global = []
            present_indices = []
            for idx, rk in enumerate(rel_keys):
                if rk in local_per_rel[ntype]:
                    present_local.append(local_per_rel[ntype][rk])
                    present_global.append(
                        global_per_rel[ntype].get(rk, local_per_rel[ntype][rk])
                    )
                    present_indices.append(idx)

            if not present_local:
                result[ntype] = x_dict[ntype]
                continue

            # Apply per-relation softmax gates
            if len(present_local) > 1:
                gate_logits = self.relation_gates[ntype][present_indices]
                gate_weights = F.softmax(gate_logits, dim=0)  # [n_present]
                # Weighted sum: [n_present, N, D] * [n_present, 1, 1]
                local_stack = torch.stack(present_local, dim=0)   # [R, N, D]
                global_stack = torch.stack(present_global, dim=0)
                gw = gate_weights.view(-1, 1, 1)
                loc = (local_stack * gw).sum(dim=0)   # [N, D]
                glb = (global_stack * gw).sum(dim=0)
            else:
                loc = present_local[0]
                glb = present_global[0]

            # Sigmoid gate for local/global combination
            gate = torch.sigmoid(
                self.gate_proj[ntype](torch.cat([loc, glb], dim=-1))
            )  # [N, hidden_dim]
            combined = gate * loc + (1.0 - gate) * glb
            result[ntype] = self.output_proj[ntype](combined)

            # Store gate for inspection
            self.last_branch_gate[ntype] = gate.detach()

        if return_attention_weights:
            return result, attn_dict
        return result


# ------------------------------------------------------------------ #
# Heterogeneous RGAT Encoder                                           #
# ------------------------------------------------------------------ #

class HeteroRGATEncoder(nn.Module):
    """Multi-layer heterogeneous encoder with multi-scale GATConv attention.

    Changes from v1:
    - Collects attention from all layers (or configurable subset)
    - Structured attention output: Dict[int, Dict[str, Tensor]]
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
        collect_attention_layers: Optional[Tuple[int, ...]] = None,
    ) -> None:
        super().__init__()
        self.node_types = node_types
        self.edge_types = edge_types
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.dropout = dropout
        # Which layers to collect attention from (None = all)
        self.collect_attention_layers = collect_attention_layers

        self.leiden_embedding = nn.Embedding(
            num_leiden_ids, leiden_embed_dim, padding_idx=0,
        )

        self.input_proj = nn.ModuleDict()
        for ntype in node_types:
            if ntype not in scalar_dims:
                raise ValueError(
                    f"Missing scalar_dims for node type '{ntype}'. "
                    f"Available: {list(scalar_dims.keys())}"
                )
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

    def _should_collect_attn(self, layer_idx: int) -> bool:
        """Whether to collect attention at a given layer."""
        if self.collect_attention_layers is None:
            return True
        return layer_idx in self.collect_attention_layers

    def forward(
        self, data: HeteroData, return_attention_weights: bool = False,
    ):
        # ---- Input projection ----
        x_dict: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            store = data[ntype]
            if not hasattr(store, "x_scalar"):
                raise ValueError(f"Node type '{ntype}' missing required field 'x_scalar'")
            if not hasattr(store, "x_text"):
                raise ValueError(f"Node type '{ntype}' missing required field 'x_text'")
            if not hasattr(store, "leiden_ids"):
                raise ValueError(f"Node type '{ntype}' missing required field 'leiden_ids'")
            x_cat = torch.cat([
                store.x_scalar,
                store.x_text,
                self.leiden_embedding(store.leiden_ids),
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

        # Structured attention: {layer_idx: {key: alpha_tensor}}
        all_attn: Dict[int, Dict[str, Tensor]] = {}

        for i, (conv, norm_dict) in enumerate(zip(self.convs, self.norms)):
            x_residual = x_dict

            collect = return_attention_weights and self._should_collect_attn(i)
            if collect:
                x_dict, layer_attn = conv(
                    x_dict, edge_index_dict, global_ei,
                    return_attention_weights=True,
                )
                all_attn[i] = layer_attn
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
# Attention Diversity Loss (v2)                                        #
# ------------------------------------------------------------------ #

class AttentionDiversityLoss(nn.Module):
    """Output-based diversity loss with three terms:

    1. **Orthogonality**: penalise cosine similarity between attention heads
    2. **Variance**: penalise low variance within each head (uniform attention)
    3. **Coverage**: penalise one head dominating all edges at a destination

    Operates on gradient-connected attention tensors across multiple layers.
    Falls back to parameter-level loss when no attention is cached.
    """

    def __init__(
        self,
        ortho_weight: float = 0.5,
        variance_weight: float = 0.3,
        coverage_weight: float = 0.2,
        num_layers: int = 3,
    ) -> None:
        super().__init__()
        self.ortho_weight = ortho_weight
        self.variance_weight = variance_weight
        self.coverage_weight = coverage_weight
        self.num_layers = max(num_layers, 1)
        # Set by training loop before calling forward
        self.cached_attn: Dict[int, Dict[str, Tensor]] = {}

    def forward(self, encoder: HeteroRGATEncoder) -> Tensor:
        if self.cached_attn:
            return self._output_based_loss()
        return self._param_level_loss(encoder)

    def _output_based_loss(self) -> Tensor:
        """Compute diversity loss on actual attention outputs (gradient-connected)."""
        device = None
        for layer_attn in self.cached_attn.values():
            for t in layer_attn.values():
                device = t.device
                break
            if device is not None:
                break
        if device is None:
            return torch.tensor(0.0)

        total_loss = torch.tensor(0.0, device=device)
        n_terms = 0

        for layer_idx, layer_attn in self.cached_attn.items():
            # Deeper layers get higher weight: 0.5 at layer 0, 1.0 at last
            if self.num_layers > 1:
                layer_w = 0.5 + 0.5 * (layer_idx / (self.num_layers - 1))
            else:
                layer_w = 1.0

            for _key, attn in layer_attn.items():
                if attn.dim() != 2 or attn.size(1) < 2 or attn.size(0) < 2:
                    continue

                H = attn.size(1)    # num heads
                E = attn.size(0)    # num edges
                heads = attn.T      # [H, E]

                # ---- 1. Head orthogonality (primary) ----
                heads_norm = F.normalize(heads, p=2, dim=1, eps=1e-8)
                sim = torch.mm(heads_norm, heads_norm.t())  # [H, H]
                mask = ~torch.eye(H, dtype=torch.bool, device=device)
                ortho_loss = sim[mask].abs().mean() if mask.any() else torch.tensor(0.0, device=device)

                # ---- 2. Head variance maximisation ----
                # High variance within a head means peaked attention (good)
                # Penalise low variance
                head_var = heads.var(dim=1)       # [H]
                # Normalise by max possible variance for attention in [0,1]
                variance_loss = (1.0 - head_var.clamp(max=1.0)).mean()

                # ---- 3. Complementary coverage ----
                # For each edge, which head has the max attention?
                # Penalise if one head dominates across all edges.
                max_head_per_edge = attn.argmax(dim=1)  # [E]
                # Compute fraction of edges won by each head
                coverage_counts = torch.zeros(H, device=device)
                for h in range(H):
                    coverage_counts[h] = (max_head_per_edge == h).float().sum()
                coverage_dist = coverage_counts / (E + 1e-8)
                # Ideal = uniform (1/H each). Penalise deviation.
                uniform_target = 1.0 / H
                coverage_loss = ((coverage_dist - uniform_target).abs()).mean()

                edge_loss = (
                    self.ortho_weight * ortho_loss
                    + self.variance_weight * variance_loss
                    + self.coverage_weight * coverage_loss
                )

                if torch.isfinite(edge_loss):
                    total_loss = total_loss + layer_w * edge_loss
                    n_terms += 1

        if n_terms > 0:
            total_loss = total_loss / n_terms
        if not torch.isfinite(total_loss):
            total_loss = torch.tensor(0.0, device=device)
        return total_loss

    def _param_level_loss(self, encoder: HeteroRGATEncoder) -> Tensor:
        """Fallback: penalise similar head parameters (not gradient-connected
        to attention outputs, but better than nothing)."""
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
# Relation-Specific Decoder                                            #
# ------------------------------------------------------------------ #

class RelationDecoder(nn.Module):
    """Relation-specific link prediction decoder.

    Supports two modes:
    - ``distmult``: score = (z_src * r_rel * z_dst).sum(-1)
    - ``bilinear``:  score = z_src^T W_rel z_dst

    Each supervised triplet gets its own relation parameters.
    """

    def __init__(
        self,
        hidden_dim: int,
        supervised_triplets: List[Tuple[str, str, str]],
        decoder_type: str = "distmult",
    ) -> None:
        super().__init__()
        if not supervised_triplets:
            raise ValueError("RelationDecoder requires at least one supervised triplet")

        self.decoder_type = decoder_type
        self.hidden_dim = hidden_dim
        self._triplet_keys: Dict[Tuple[str, str, str], str] = {}

        if decoder_type == "distmult":
            self.rel_emb = nn.ParameterDict()
            for triplet in supervised_triplets:
                key = f"{triplet[0]}__{triplet[1]}__{triplet[2]}"
                self._triplet_keys[triplet] = key
                self.rel_emb[key] = nn.Parameter(torch.ones(hidden_dim))
        elif decoder_type == "bilinear":
            self.rel_mat = nn.ParameterDict()
            for triplet in supervised_triplets:
                key = f"{triplet[0]}__{triplet[1]}__{triplet[2]}"
                self._triplet_keys[triplet] = key
                w = torch.empty(hidden_dim, hidden_dim)
                nn.init.xavier_uniform_(w)
                self.rel_mat[key] = nn.Parameter(w)
        else:
            raise ValueError(f"Unknown decoder_type: {decoder_type!r}. Use 'distmult' or 'bilinear'.")

    def forward(
        self,
        z_src: Tensor,
        z_dst: Tensor,
        triplet: Tuple[str, str, str],
    ) -> Tensor:
        key = self._triplet_keys.get(triplet)
        if key is None:
            raise ValueError(
                f"Triplet {triplet} not registered in decoder. "
                f"Registered: {list(self._triplet_keys.keys())}"
            )

        if self.decoder_type == "distmult":
            r = self.rel_emb[key]
            return (z_src * r * z_dst).sum(dim=-1)
        else:
            W = self.rel_mat[key]
            return (z_src @ W * z_dst).sum(dim=-1)


# ------------------------------------------------------------------ #
# Legacy LinkPredictor (kept for backward compatibility / ablation)     #
# ------------------------------------------------------------------ #

class LinkPredictor(nn.Module):
    """Relation-agnostic dot-product decoder (baseline for ablation)."""

    def forward(self, z_src: Tensor, z_dst: Tensor) -> Tensor:
        return (z_src * z_dst).sum(dim=-1)


# ------------------------------------------------------------------ #
# Auxiliary Supervision Heads                                          #
# ------------------------------------------------------------------ #

class SameRepoHead(nn.Module):
    """Predict whether two nodes belong to the same repository.

    Takes |z_src - z_dst| as input and outputs a logit.
    """

    def __init__(self, hidden_dim: int) -> None:
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(hidden_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, z_src: Tensor, z_dst: Tensor) -> Tensor:
        diff = (z_src - z_dst).abs()
        return self.mlp(diff).squeeze(-1)


class DegreeBucketHead(nn.Module):
    """Predict a discretised log-degree bucket for a node.

    Returns logits over `num_buckets` classes.
    """

    def __init__(self, hidden_dim: int, num_buckets: int = 6) -> None:
        super().__init__()
        self.linear = nn.Linear(hidden_dim, num_buckets)

    def forward(self, z: Tensor) -> Tensor:
        return self.linear(z)


# ------------------------------------------------------------------ #
# Diversity metrics computation (for logging, not loss)                #
# ------------------------------------------------------------------ #

def compute_diversity_metrics(
    attn_dict: Dict[int, Dict[str, Tensor]],
) -> Dict[str, float]:
    """Compute per-edge-type head diversity metrics for logging.

    Returns a flat dict with keys like:
        "layer0/local|src|rel|dst/cosine_sim"
        "layer0/local|src|rel|dst/mean_entropy"
        "layer0/local|src|rel|dst/mean_variance"
        "layer0/local|src|rel|dst/avg_pairwise_sim"

    All computations are detached (no gradient flow).
    """
    import numpy as np

    metrics: Dict[str, float] = {}

    for layer_idx, layer_attn in attn_dict.items():
        for key, attn in layer_attn.items():
            if attn.dim() != 2 or attn.size(1) < 2:
                continue

            attn_np = attn.detach().cpu()
            H = attn_np.size(1)
            heads = attn_np.T  # [H, E]

            prefix = f"layer{layer_idx}/{key}"

            # 1. Mean pairwise cosine similarity
            heads_norm = F.normalize(heads, p=2, dim=1, eps=1e-8)
            sim = torch.mm(heads_norm, heads_norm.t())
            mask = ~torch.eye(H, dtype=torch.bool)
            if mask.any():
                cos_sim = sim[mask].abs().mean().item()
                avg_sim = sim[mask].mean().item()
            else:
                cos_sim = 0.0
                avg_sim = 0.0
            metrics[f"{prefix}/cosine_sim"] = cos_sim
            metrics[f"{prefix}/avg_pairwise_sim"] = avg_sim

            # 2. Mean entropy per head
            entropies = []
            for h in range(H):
                p = F.softmax(heads[h], dim=0)
                ent = -(p * torch.log(p + 1e-8)).sum()
                max_ent = math.log(heads.size(1)) if heads.size(1) > 1 else 1.0
                entropies.append((ent / max_ent).item())
            metrics[f"{prefix}/mean_entropy"] = float(np.mean(entropies))

            # 3. Mean variance per head
            variances = []
            for h in range(H):
                variances.append(heads[h].var().item())
            metrics[f"{prefix}/mean_variance"] = float(np.mean(variances))

    return metrics
