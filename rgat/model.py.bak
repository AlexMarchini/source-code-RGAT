"""
rgat.model – Heterogeneous RGAT encoder and link-prediction decoder.

Architecture
------------
1. **Input projection**: per-type ``nn.Linear`` that maps the concatenation
   of (scalar features ∥ sentence embedding ∥ leiden embedding) to a uniform
   ``hidden_dim``.
2. **Multi-scale GATv2 message passing**: stacked ``MultiScaleHeteroConv``
   layers that split attention heads across two context scales:

   * **Local** (``num_heads // 2`` heads): attends to direct 1-hop
     neighbours, capturing fine-grained structural cues.
   * **Global** (``num_heads - num_heads // 2`` heads): applies a second
     1-hop conv *on top of the local output*, giving each node an effective
     2-hop receptive field for broader context.

   Both branches produce ``hidden_dim``-sized tensors, concatenated and
   re-projected by a learned linear per node type.  This partitioning forces
   local and global heads to specialise rather than converging to the same
   attention distribution.

3. **Link predictor**: dot-product decoder over source / target node
   embeddings → logits for ``BCEWithLogitsLoss``.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from torch_geometric.data import HeteroData
from torch_geometric.nn import GATv2Conv, HeteroConv


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _diversify_gatv2_heads(conv: "GATv2Conv") -> None:  # noqa: F821
    """Break head-parameter symmetry with per-head diverse initialisation.

    Default PyTorch init is symmetric across heads.  Two heads that start at
    the same value stay at the same value throughout training (gradient
    symmetry).  This function cycles through three distinct initialisation
    strategies per head (mirroring the PoC) to ensure strong asymmetry from
    the start.
    """
    H = conv.att.shape[1]       # number of heads
    C = conv.att.shape[2]       # out_channels per head
    with torch.no_grad():
        for h in range(H):
            if h % 3 == 0:
                # Strategy 1: Xavier + noisy perturbation
                nn.init.xavier_uniform_(conv.att.data[0, h:h + 1])
                conv.att.data[0, h] += torch.randn_like(conv.att.data[0, h]) * 0.1
                for lin in (conv.lin_l, conv.lin_r):
                    nn.init.xavier_uniform_(lin.weight.data[h * C:(h + 1) * C])
                    lin.weight.data[h * C:(h + 1) * C] += (
                        torch.randn_like(lin.weight.data[h * C:(h + 1) * C]) * 0.1
                    )
            elif h % 3 == 1:
                # Strategy 2: Uniform with head-scaled range
                scale = 0.2 + h * 0.1
                nn.init.uniform_(conv.att.data[0, h:h + 1], -scale, scale)
                for lin in (conv.lin_l, conv.lin_r):
                    nn.init.uniform_(lin.weight.data[h * C:(h + 1) * C], -scale, scale)
            else:
                # Strategy 3: Normal with head-specific std
                std = 0.2 + h * 0.05
                nn.init.normal_(conv.att.data[0, h:h + 1], mean=0, std=std)
                for lin in (conv.lin_l, conv.lin_r):
                    nn.init.normal_(lin.weight.data[h * C:(h + 1) * C], mean=0, std=std)


def _structural_global_edges(
    edge_index: Tensor,
    n_src: int,
    n_dst: int,
    max_per_node: int = 6,
    walk_len: int = 3,
    rng_seed: int = 0,
) -> Tensor:
    """Augment a homogeneous edge index with *structural-role* edges.

    2-hop paths through hub nodes (e.g. ``__init__``, ``append``) produce
    neighbourhoods dominated by the same few nodes for every source — the
    softmax collapses to uniform and attention becomes uninformative.

    This function instead adds edges that connect nodes sharing a *similar
    structural role*, mirroring the PoC's ``_create_extended_edges``:

    **Degree-bucket edges** — nodes are bucketed by their log₂(degree).
    Nodes in the same bucket play a similar role (both leaves, both hubs,
    etc.).  A bounded sample of within-bucket pairs is added.

    **Random-walk co-occurrence edges** — short random walks from each node
    collect a set of "reachable contexts".  Two nodes that frequently appear
    in the same walk are likely to be structurally equivalent.  The start
    node is connected to non-adjacent nodes encountered during the walk.

    Both edge sets are capped at ``max_per_node`` new neighbours per node to
    prevent the global graph from becoming too dense.

    For heterogeneous edge types (``n_src ≠ n_dst``) the original index is
    returned unchanged.
    """
    if n_src != n_dst or edge_index.size(1) == 0:
        return edge_index   # structural roles only defined for same-type edges

    import random
    rng = random.Random(rng_seed)

    N = n_src
    # Build adjacency list (out-edges) and in-degree
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
    added: Dict[int, int] = {i: 0 for i in range(N)}  # budget tracker per node

    new_src: List[int] = []
    new_dst: List[int] = []

    def _add(a: int, c: int) -> bool:
        """Add edge a→c if new and both nodes have budget remaining."""
        if (
            c != a
            and (a, c) not in existing
            and added[a] < max_per_node
        ):
            new_src.append(a)
            new_dst.append(c)
            existing.add((a, c))
            added[a] += 1
            return True
        return False

    # ── 1) Degree-bucket edges ─────────────────────────────────────────
    # Group nodes by log2(out_degree+1) bucket (coarse structural role)
    import math
    buckets: Dict[int, List[int]] = {}
    for node in range(N):
        b = int(math.log2(out_deg[node] + 1))
        buckets.setdefault(b, []).append(node)

    for bucket_nodes in buckets.values():
        if len(bucket_nodes) < 2:
            continue
        # Shuffle and sample pairs — bounded by max_per_node budget
        shuffled = bucket_nodes[:]
        rng.shuffle(shuffled)
        # Pair consecutive nodes in the shuffled list
        for i in range(0, len(shuffled) - 1, 2):
            a, c = shuffled[i], shuffled[i + 1]
            _add(a, c)
            _add(c, a)  # bidirectional structural similarity

    # ── 2) Random-walk co-occurrence edges ────────────────────────────
    # For each node, run a short walk and connect start to visited nodes
    # that it doesn't already reach (skipping direct neighbours)
    for start in range(N):
        if not adj[start]:
            continue   # isolated node
        cur = start
        visited: List[int] = []
        for _ in range(walk_len):
            if not adj[cur]:
                break
            cur = rng.choice(adj[cur])
            if cur != start:
                visited.append(cur)
        # Connect start to non-adjacent visited nodes only
        for v in visited:
            if (start, v) not in existing:
                _add(start, v)

    if not new_src:
        return edge_index

    extra = torch.tensor(
        [new_src, new_dst], dtype=edge_index.dtype, device=edge_index.device
    )
    return torch.cat([edge_index, extra], dim=1)


# ────────────────────────────────────────────────────────────────────────
# Multi-scale heterogeneous convolution block
# ────────────────────────────────────────────────────────────────────────

class MultiScaleHeteroConv(nn.Module):
    """Drop-in replacement for ``HeteroConv`` with two-scale attention.

    Splits ``num_heads`` attention heads across a *local* and a *global*
    branch that both start from the **same input features** ``x_dict`` but
    see different neighbourhoods:

    * **Local branch** (``num_heads // 2`` heads):
      Standard 1-hop ``GATv2Conv`` over direct neighbours.

    * **Global branch** (``num_heads - num_heads // 2`` heads):
      ``GATv2Conv`` over an *extended* edge set that includes 2-hop
      connections (pre-computed by the encoder) — giving each node an
      effective 2-hop receptive field.  The global convolutions also use a
      sharper LeakyReLU slope (``lrelu_slope * 1.5``) to bias them toward
      attending to different features than the local branch.
      Starting from the original features rather than local output removes
      the trivial correlation between branches.

    * **Per-head diverse initialisation**: unique scale factors and noise are
      applied to each head's parameter slices after construction to break the
      gradient symmetry that would otherwise keep heads identical throughout
      training.

    * **Scale combine** (per node type ``Linear(2·hidden → hidden)``):
      Learns to weight local vs. global context.

    Parameters
    ----------
    edge_types : list of (src, rel, dst) tuples
    node_types : list of str
    hidden_dim : int
    num_heads : int
        Total number of attention heads, split evenly between branches.
    dropout : float
    lrelu_slope : float
        LeakyReLU negative slope for local branch.  Global uses
        ``lrelu_slope * 1.5`` (matches the PoC ``negative_slope * 1.5``).
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

        local_dict: Dict[Tuple[str, str, str], GATv2Conv] = {}
        global_dict: Dict[Tuple[str, str, str], GATv2Conv] = {}
        for et in edge_types:
            local_dict[et] = GATv2Conv(
                in_channels=hidden_dim,
                out_channels=local_head_dim,
                heads=local_heads,
                concat=True,
                dropout=dropout,
                add_self_loops=False,
                share_weights=False,
                negative_slope=lrelu_slope,
            )
            global_dict[et] = GATv2Conv(
                in_channels=hidden_dim,
                out_channels=global_head_dim,
                heads=global_heads,
                concat=True,
                dropout=dropout,
                add_self_loops=False,
                share_weights=False,
                negative_slope=global_slope,   # ← sharper slope biases to different features
            )

        self.local_conv = HeteroConv(local_dict, aggr="sum")
        self.global_conv = HeteroConv(global_dict, aggr="sum")

        # Learned combination: [local ∥ global] → hidden_dim
        self.scale_combine = nn.ModuleDict({
            ntype: nn.Linear(2 * hidden_dim, hidden_dim, bias=False)
            for ntype in node_types
        })
        self.node_types = node_types

        # ── Break head symmetry ─────────────────────────────────────────
        for sub_conv in (
            list(self.local_conv.convs.values())
            + list(self.global_conv.convs.values())
        ):
            _diversify_gatv2_heads(sub_conv)

    def forward(
        self,
        x_dict: Dict[str, "Tensor"],
        edge_index_dict: Dict,
        global_edge_index_dict: Optional[Dict] = None,
    ) -> Dict[str, "Tensor"]:
        """Run both branches from ``x_dict`` with distinct edge sets.

        Parameters
        ----------
        x_dict : dict
            Current node embeddings ``{ntype: Tensor[N, hidden_dim]}``.
        edge_index_dict : dict
            1-hop edge indices for local branch.
        global_edge_index_dict : dict, optional
            2-hop-augmented edge indices for global branch.  Falls back
            to ``edge_index_dict`` when ``None``.
        """
        global_ei = global_edge_index_dict if global_edge_index_dict is not None else edge_index_dict

        # ── Local pass (1-hop, from x_dict) ───────────────────────────
        h_local = self.local_conv(x_dict, edge_index_dict)
        for ntype in self.node_types:
            if ntype not in h_local:
                h_local[ntype] = x_dict[ntype]

        # ── Global pass (2-hop edges, also from x_dict) ────────────────
        # Starting from x_dict (not h_local) removes the trivial correlation
        # between branches — matching the PoC design.
        h_global = self.global_conv(x_dict, global_ei)

        # ── Combine scales ─────────────────────────────────────────────
        out: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            loc = h_local.get(ntype, x_dict[ntype])
            glb = h_global.get(ntype, x_dict[ntype])
            out[ntype] = self.scale_combine[ntype](
                torch.cat([loc, glb], dim=-1)
            )
        return out


# ────────────────────────────────────────────────────────────────────────
# Heterogeneous RGAT Encoder
# ────────────────────────────────────────────────────────────────────────

class HeteroRGATEncoder(nn.Module):
    """Multi-layer heterogeneous graph encoder with multi-scale GATv2 attention.

    Each message-passing layer is a :class:`MultiScaleHeteroConv` that splits
    the ``num_heads`` attention heads across two branches:

    * **Local heads** (``num_heads // 2``): attend to direct 1-hop neighbours.
    * **Global heads** (remaining): operate on the local output, capturing
      an effective 2-hop neighbourhood context.

    This partitioning prevents all heads from collapsing to the same
    attention distribution, as each branch is structurally forced to look at
    a different neighbourhood depth.

    Parameters
    ----------
    node_types : list[str]
        All node type labels.
    edge_types : list[tuple[str, str, str]]
        All ``(src, rel, dst)`` triplets (including reverse edges).
    scalar_dims : dict[str, int]
        Per-type scalar feature dimension.
    sentence_dim : int
        Dimension of pre-computed sentence embeddings (384 for MiniLM).
    leiden_embed_dim : int
        Dimension of the Leiden community embedding.
    num_leiden_ids : int
        Vocabulary size for the Leiden embedding table.
    hidden_dim : int
        Internal representation dimension (same for all types after projection).
    num_heads : int
        Total number of attention heads, split evenly between local and global
        branches.  Must be even and ≥ 2.
    num_layers : int
        Number of stacked ``MultiScaleHeteroConv`` layers.
    dropout : float
        Dropout probability.
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

        # ── Leiden community embedding (shared across all node types) ──
        self.leiden_embedding = nn.Embedding(
            num_embeddings=num_leiden_ids,
            embedding_dim=leiden_embed_dim,
            padding_idx=0,   # index 0 = isolate bucket (-1 remapped)
        )

        # ── Per-type input projection ──────────────────────────────────
        self.input_proj = nn.ModuleDict()
        for ntype in node_types:
            in_dim = scalar_dims[ntype] + sentence_dim + leiden_embed_dim
            self.input_proj[ntype] = nn.Linear(in_dim, hidden_dim)

        # ── Stacked MultiScaleHeteroConv layers ───────────────────────
        self.convs = nn.ModuleList()
        self.norms = nn.ModuleList()

        for _ in range(num_layers):
            self.convs.append(
                MultiScaleHeteroConv(
                    edge_types=edge_types,
                    node_types=node_types,
                    hidden_dim=hidden_dim,
                    num_heads=num_heads,
                    dropout=dropout,
                )
            )

            # Per-type LayerNorm
            norm_dict = nn.ModuleDict({
                ntype: nn.LayerNorm(hidden_dim) for ntype in node_types
            })
            self.norms.append(norm_dict)

    def forward(self, data: HeteroData) -> Dict[str, Tensor]:
        """Run the encoder.

        Parameters
        ----------
        data : HeteroData
            Must have ``x_scalar``, ``x_text``, ``leiden_ids``, and
            ``edge_index`` populated for every type.

        Returns
        -------
        dict[str, Tensor]
            Node embeddings ``{node_type: Tensor[N_type, hidden_dim]}``.
        """
        # ── Assemble per-type input features ───────────────────────────
        x_dict: Dict[str, Tensor] = {}
        for ntype in self.node_types:
            x_scalar = data[ntype].x_scalar              # [N, scalar_dim]
            x_text = data[ntype].x_text                  # [N, sentence_dim]
            leiden_ids = data[ntype].leiden_ids            # [N]
            x_leiden = self.leiden_embedding(leiden_ids)   # [N, leiden_embed_dim]

            x_cat = torch.cat([x_scalar, x_text, x_leiden], dim=-1)
            x_dict[ntype] = self.input_proj[ntype](x_cat)

        # ── Apply ReLU after projection ────────────────────────────────
        x_dict = {k: F.elu(v) for k, v in x_dict.items()}

        # ── Message-passing layers ─────────────────────────────────────
        edge_index_dict = {
            et: data[et].edge_index
            for et in self.edge_types
            if hasattr(data[et], "edge_index")
        }

        # ── Build / retrieve structural-role global edge index (cached) ─
        # Compute once per unique data object using its id as cache key.
        data_id = id(data)
        if not hasattr(self, "_global_ei_cache") or self._global_ei_cache_id != data_id:
            global_edge_index_dict: Dict = {}
            for et, ei in edge_index_dict.items():
                src_type, _, dst_type = et
                n_src = data[src_type].num_nodes
                n_dst = data[dst_type].num_nodes
                global_edge_index_dict[et] = _structural_global_edges(ei, n_src, n_dst)
            self._global_ei_cache: Dict = global_edge_index_dict  # type: ignore[assignment]
            self._global_ei_cache_id: int = data_id
        else:
            global_edge_index_dict = self._global_ei_cache

        for i, (conv, norm_dict) in enumerate(zip(self.convs, self.norms)):
            # Residual connection
            x_residual = x_dict

            x_dict = conv(x_dict, edge_index_dict, global_edge_index_dict)

            # LayerNorm + residual + activation + dropout
            # Skip ReLU on the last layer so embeddings can have negative
            # values (needed for dot-product decoding to produce neg logits).
            is_last = (i == len(self.convs) - 1)
            new_x: Dict[str, Tensor] = {}
            for ntype in self.node_types:
                if ntype in x_dict:
                    h = norm_dict[ntype](x_dict[ntype])
                    h = h + x_residual[ntype]         # residual
                    if not is_last:
                        h = F.elu(h)
                    h = F.dropout(h, p=self.dropout, training=self.training)
                    new_x[ntype] = h
                else:
                    # Node type might not appear as a target in any edge
                    new_x[ntype] = x_residual[ntype]

            x_dict = new_x

        # ── L2-normalize output embeddings ─────────────────────────────
        x_dict = {k: F.normalize(v, p=2, dim=-1) for k, v in x_dict.items()}

        return x_dict


# ────────────────────────────────────────────────────────────────────────
# Attention Diversity Loss
# ────────────────────────────────────────────────────────────────────────

class AttentionDiversityLoss(nn.Module):
    """Enhanced diversity loss combining three objectives on actual attention
    output distributions (not just parameters).

    1. **Entropy**: penalises peaked attention (encourages each head to
       spread weight across multiple neighbours).
    2. **Head orthogonality**: penalises high cosine similarity between
       pairs of attention heads so they attend to different neighbours.
    3. **Gini sparsity**: alternates between encouraging sparse attention
       (even heads) and uniform attention (odd heads) to enforce
       complementary specialisations.

    When ``cached_attn`` is empty (i.e. not yet populated by the training
    loop) this falls back to the parameter-level orthogonality penalty.
    """

    def __init__(self) -> None:
        super().__init__()
        # Filled by the training loop each forward pass
        self.cached_attn: Dict[str, Tensor] = {}

    @staticmethod
    def _gini(x: Tensor) -> Tensor:
        """Differentiable Gini coefficient of a 1-D distribution."""
        sorted_x, _ = torch.sort(x)
        n = sorted_x.size(0)
        idx = torch.arange(1, n + 1, dtype=torch.float, device=x.device)
        total = sorted_x.sum()
        if total < 1e-12:
            return torch.tensor(0.0, device=x.device)
        gini = (2.0 * (idx * sorted_x).sum()) / (n * total) - (n + 1.0) / n
        return torch.clamp(gini, 0.0, 1.0)

    def forward(self, encoder: "HeteroRGATEncoder") -> Tensor:  # type: ignore[override]
        # ── If we have cached attention outputs, use output-based loss ──
        if self.cached_attn:
            return self._output_based_loss(encoder)
        # ── Fallback: parameter-level orthogonality ────────────────────
        return self._param_level_loss(encoder)

    def _output_based_loss(self, encoder: "HeteroRGATEncoder") -> Tensor:
        device = next(encoder.parameters()).device
        total_loss = torch.tensor(0.0, device=device)
        n_types = 0

        num_heads = encoder.num_heads

        for _key, attn_tensor in self.cached_attn.items():
            # attn_tensor: [num_edges, num_heads]
            if attn_tensor.dim() != 2 or attn_tensor.size(1) < 2:
                continue
            if attn_tensor.size(0) < 2:
                continue

            H = attn_tensor.size(1)
            heads = attn_tensor.T  # [H, num_edges]

            # ── 1. Entropy loss (encourage spread within each head) ────
            entropy_loss = torch.tensor(0.0, device=device)
            for h_idx in range(H):
                p = F.softmax(heads[h_idx], dim=0)
                ent = -(p * torch.log(p + 1e-8)).sum()
                max_ent = torch.log(torch.tensor(float(heads.size(1)), device=device))
                entropy_loss = entropy_loss + (1.0 - ent / max_ent)

            # ── 2. Head orthogonality loss ─────────────────────────────
            ortho_loss = torch.tensor(0.0, device=device)
            heads_norm = F.normalize(heads, p=2, dim=1, eps=1e-8)
            sim = torch.mm(heads_norm, heads_norm.t())
            mask = ~torch.eye(H, dtype=torch.bool, device=device)
            if mask.any():
                ortho_loss = sim[mask].abs().mean()

            # ── 3. Gini sparsity loss (alternate per head) ─────────────
            gini_loss = torch.tensor(0.0, device=device)
            for h_idx in range(H):
                p = F.softmax(heads[h_idx], dim=0)
                g = self._gini(p)
                if h_idx % 2 == 0:
                    gini_loss = gini_loss + (1.0 - g)   # encourage sparse
                else:
                    gini_loss = gini_loss + g            # encourage uniform

            edge_loss = 0.3 * entropy_loss + 0.4 * ortho_loss + 0.3 * gini_loss
            if torch.isfinite(edge_loss):
                total_loss = total_loss + edge_loss
                n_types += 1

        if n_types > 0:
            total_loss = total_loss / n_types
        if not torch.isfinite(total_loss):
            total_loss = torch.tensor(0.0, device=device)
        return total_loss

    def _param_level_loss(self, encoder: "HeteroRGATEncoder") -> Tensor:
        """Fallback: cosine-similarity penalty on parameter slices."""
        terms: List[Tensor] = []

        for ms_conv in encoder.convs:
            for hetero_conv in (ms_conv.local_conv, ms_conv.global_conv):
                for sub_conv in hetero_conv.convs.values():
                    H = sub_conv.att.shape[1]
                    if H < 2:
                        continue

                    mask = ~torch.eye(H, dtype=torch.bool, device=sub_conv.att.device)

                    heads = sub_conv.att[0]          # [H, C]
                    heads_norm = F.normalize(heads, p=2, dim=1, eps=1e-8)
                    sim = torch.mm(heads_norm, heads_norm.t())
                    terms.append(sim[mask].abs().mean())

                    for lin in (sub_conv.lin_l, sub_conv.lin_r):
                        w = lin.weight
                        H_C, in_dim = w.shape
                        C = H_C // H
                        w_per_head = w.view(H, C * in_dim)
                        w_norm = F.normalize(w_per_head, p=2, dim=1, eps=1e-8)
                        sim_w = torch.mm(w_norm, w_norm.t())
                        terms.append(sim_w[mask].abs().mean())

        if not terms:
            device = next(encoder.parameters()).device
            return torch.tensor(0.0, device=device)

        return torch.stack(terms).mean()


# ────────────────────────────────────────────────────────────────────────
# Link Predictor (dot-product decoder)
# ────────────────────────────────────────────────────────────────────────

class LinkPredictor(nn.Module):
    """Dot-product link predictor.

    Given source and target node embeddings, returns a scalar logit per
    candidate edge.  Designed to be paired with ``BCEWithLogitsLoss``.
    """

    def forward(
        self,
        z_src: Tensor,
        z_dst: Tensor,
    ) -> Tensor:
        """Compute link logits.

        Parameters
        ----------
        z_src : Tensor[B, D]
            Source node embeddings for the candidate edges.
        z_dst : Tensor[B, D]
            Target node embeddings for the candidate edges.

        Returns
        -------
        Tensor[B]
            Scalar logits (higher = more likely linked).
        """
        return (z_src * z_dst).sum(dim=-1)
