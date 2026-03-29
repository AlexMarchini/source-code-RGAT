"""
rgat.training – Training loop, evaluation, and metric computation.

Provides a full-batch training pipeline for masked link prediction on a
heterogeneous graph.  Reports per-relation and aggregate metrics including
loss, accuracy, ROC-AUC, and average precision.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
from torch import Tensor
from torch_geometric.data import HeteroData

from sklearn.metrics import average_precision_score, roc_auc_score

from rgat.config import RGATConfig
from rgat.model import AttentionDiversityLoss, HeteroRGATEncoder, LinkPredictor


# ── Metric container ───────────────────────────────────────────────────

class Metrics:
    """Container for per-relation link-prediction metrics."""

    def __init__(self) -> None:
        self.loss: float = 0.0
        self.accuracy: float = 0.0
        self.roc_auc: float = 0.0
        self.avg_precision: float = 0.0
        self.n_edges: int = 0

    def __repr__(self) -> str:
        return (
            f"loss={self.loss:.4f}  acc={self.accuracy:.4f}  "
            f"AUC={self.roc_auc:.4f}  AP={self.avg_precision:.4f}  "
            f"n={self.n_edges:,}"
        )


# ── Core evaluation function ──────────────────────────────────────────

def evaluate_link_prediction(
    encoder: HeteroRGATEncoder,
    predictor: LinkPredictor,
    data: HeteroData,
    supervised_triplets: List[Tuple[str, str, str]],
    criterion: nn.Module,
) -> Tuple[Dict[Tuple[str, str, str], Metrics], float]:
    """Evaluate link prediction on a single data split.

    Parameters
    ----------
    encoder : HeteroRGATEncoder
    predictor : LinkPredictor
    data : HeteroData
        Must have ``edge_label_index`` and ``edge_label`` on supervised
        triplets.
    supervised_triplets : list
        Edge triplets to evaluate.
    criterion : nn.Module
        Loss function (``BCEWithLogitsLoss``).

    Returns
    -------
    per_relation : dict
        ``{triplet: Metrics}`` for each supervised triplet.
    total_loss : float
        Summed loss across all supervised triplets.
    """
    encoder.eval()
    predictor.eval()

    with torch.no_grad():
        z_dict = encoder(data)

    per_relation: Dict[Tuple[str, str, str], Metrics] = {}
    total_loss = 0.0

    for triplet in supervised_triplets:
        src_type, rel, dst_type = triplet
        store = data[triplet]

        if not hasattr(store, "edge_label_index") or not hasattr(store, "edge_label"):
            continue

        edge_label_index = store.edge_label_index
        edge_label = store.edge_label.float()

        if edge_label.numel() == 0:
            continue

        z_src = z_dict[src_type][edge_label_index[0]]
        z_dst = z_dict[dst_type][edge_label_index[1]]

        logits = predictor(z_src, z_dst)
        loss = criterion(logits, edge_label)

        # Detach for metric computation
        probs_np = torch.sigmoid(logits).cpu().numpy()
        logits_np = logits.cpu().numpy()
        labels_np = edge_label.cpu().numpy()
        preds_np = (probs_np >= 0.5).astype(float)

        m = Metrics()
        m.loss = loss.item()
        m.n_edges = int(edge_label.numel())
        m.accuracy = float((preds_np == labels_np).mean())

        # ROC-AUC and AP require both classes to be present
        if len(set(labels_np.tolist())) > 1:
            m.roc_auc = float(roc_auc_score(labels_np, logits_np))
            m.avg_precision = float(average_precision_score(labels_np, logits_np))
        else:
            m.roc_auc = 0.0
            m.avg_precision = 0.0

        per_relation[triplet] = m
        total_loss += loss.item()

    return per_relation, total_loss


# ── Attention diversity helper ─────────────────────────────────────────
# (parameter-level: no extra forward pass needed)

def _compute_diversity_loss(
    encoder: HeteroRGATEncoder,
    diversity_criterion: "AttentionDiversityLoss",
    train_data: "HeteroData",
) -> Tensor:
    """Capture attention outputs and compute the enhanced diversity loss.

    Runs a short hook-based forward pass on a sample of edge types to
    populate ``diversity_criterion.cached_attn``, then delegates to the
    criterion which uses output-based entropy + orthogonality + Gini terms.
    """
    import torch.nn.functional as _F

    # Collect attention weights from the first layer's local convolutions
    # by calling each sub-conv with return_attention_weights=True.
    cached: Dict[str, Tensor] = {}

    with torch.no_grad():
        # Re-use the already-projected features from the encoder cache
        # to avoid a full second forward pass.  We only need the first
        # conv layer's attention.
        x_dict_proj: Dict[str, Tensor] = {}
        for ntype in encoder.node_types:
            x_scalar = train_data[ntype].x_scalar
            x_text = train_data[ntype].x_text
            leiden_ids = train_data[ntype].leiden_ids
            x_leiden = encoder.leiden_embedding(leiden_ids)
            x_cat = torch.cat([x_scalar, x_text, x_leiden], dim=-1)
            x_dict_proj[ntype] = _F.elu(encoder.input_proj[ntype](x_cat))

        edge_index_dict = {
            et: train_data[et].edge_index
            for et in encoder.edge_types
            if hasattr(train_data[et], "edge_index")
        }

        ms_conv = encoder.convs[0]  # first layer

        # Local branch attention
        for edge_type, sub_conv in ms_conv.local_conv.convs.items():
            src_type, rel, dst_type = edge_type
            if edge_type not in edge_index_dict:
                continue
            ei = edge_index_dict[edge_type]
            result = sub_conv(
                (x_dict_proj[src_type], x_dict_proj[dst_type]), ei,
                return_attention_weights=True,
            )
            if isinstance(result, tuple) and len(result) == 2:
                _, attn_info = result
                if isinstance(attn_info, tuple) and len(attn_info) == 2:
                    _, alpha = attn_info
                    cached[f"local_{src_type}_{rel}_{dst_type}"] = alpha

        # Global branch attention
        global_ei = getattr(encoder, "_global_ei_cache", edge_index_dict)
        for edge_type, sub_conv in ms_conv.global_conv.convs.items():
            src_type, rel, dst_type = edge_type
            ei = global_ei.get(edge_type, edge_index_dict.get(edge_type))
            if ei is None:
                continue
            result = sub_conv(
                (x_dict_proj[src_type], x_dict_proj[dst_type]), ei,
                return_attention_weights=True,
            )
            if isinstance(result, tuple) and len(result) == 2:
                _, attn_info = result
                if isinstance(attn_info, tuple) and len(attn_info) == 2:
                    _, alpha = attn_info
                    cached[f"global_{src_type}_{rel}_{dst_type}"] = alpha

    diversity_criterion.cached_attn = cached
    return diversity_criterion(encoder)


# ── Training loop ──────────────────────────────────────────────────────

def train(
    encoder: HeteroRGATEncoder,
    predictor: LinkPredictor,
    train_data: HeteroData,
    val_data: HeteroData,
    config: RGATConfig,
) -> Dict[str, list]:
    """Run the full training loop.

    Parameters
    ----------
    encoder, predictor : nn.Module
    train_data, val_data : HeteroData
    config : RGATConfig

    Returns
    -------
    history : dict
        Training history with keys ``"epoch"``, ``"train_loss"``,
        ``"val_loss"``, ``"val_auc"``, ``"val_ap"``, and per-relation
        metrics.
    """
    device = torch.device(config.device)
    encoder = encoder.to(device)
    predictor = predictor.to(device)
    train_data = train_data.to(device)
    val_data = val_data.to(device)

    # ── Identify supervised triplets from train_data ───────────────────
    supervised_triplets: List[Tuple[str, str, str]] = []
    for triplet in train_data.edge_types:
        src_type, rel, dst_type = triplet
        if rel in config.supervised_relations:
            store = train_data[triplet]
            if hasattr(store, "edge_label_index"):
                supervised_triplets.append(triplet)

    if not supervised_triplets:
        raise ValueError("No supervised edge triplets with edge_label_index found")

    print(f"[training] Supervised triplets: {supervised_triplets}")
    print(f"[training] Device: {device}")

    # ── Optimizer and loss ─────────────────────────────────────────────
    params = list(encoder.parameters()) + list(predictor.parameters())
    optimizer = torch.optim.Adam(params, lr=config.lr, weight_decay=config.weight_decay)
    criterion = nn.BCEWithLogitsLoss()

    # ── Attention diversity regulariser ────────────────────────────────
    use_diversity = config.diversity_loss_weight > 0
    diversity_criterion = AttentionDiversityLoss().to(device) if use_diversity else None

    # ── History ────────────────────────────────────────────────────────
    history: Dict[str, list] = {
        "epoch": [],
        "train_loss": [],
        "val_loss": [],
        "val_auc": [],
        "val_ap": [],
    }

    best_val_auc = 0.0
    patience_counter = 0

    # ── Training epochs ────────────────────────────────────────────────
    for epoch in range(1, config.epochs + 1):
        t0 = time.perf_counter()
        encoder.train()
        predictor.train()
        optimizer.zero_grad()

        # Forward
        z_dict = encoder(train_data)

        # Compute loss across all supervised triplets
        total_loss = torch.tensor(0.0, device=device, requires_grad=True)
        for triplet in supervised_triplets:
            src_type, rel, dst_type = triplet
            store = train_data[triplet]
            edge_label_index = store.edge_label_index
            edge_label = store.edge_label.float()

            if edge_label.numel() == 0:
                continue

            z_src = z_dict[src_type][edge_label_index[0]]
            z_dst = z_dict[dst_type][edge_label_index[1]]

            logits = predictor(z_src, z_dst)
            loss = criterion(logits, edge_label)
            total_loss = total_loss + loss

        # ── Attention diversity regularisation (after warmup) ────────
        if use_diversity and epoch > config.diversity_warmup_epochs:
            div_loss = _compute_diversity_loss(encoder, diversity_criterion, train_data)
            total_loss = total_loss + config.diversity_loss_weight * div_loss

        total_loss.backward()
        optimizer.step()

        elapsed = time.perf_counter() - t0

        history["epoch"].append(epoch)
        history["train_loss"].append(total_loss.item())

        # ── Validation ─────────────────────────────────────────────────
        if epoch % config.val_every == 0 or epoch == 1:
            val_metrics, val_loss = evaluate_link_prediction(
                encoder, predictor, val_data, supervised_triplets, criterion,
            )

            # Aggregate val metrics (weighted by edge count)
            total_edges = sum(m.n_edges for m in val_metrics.values())
            if total_edges > 0:
                agg_auc = sum(m.roc_auc * m.n_edges for m in val_metrics.values()) / total_edges
                agg_ap = sum(m.avg_precision * m.n_edges for m in val_metrics.values()) / total_edges
            else:
                agg_auc = 0.0
                agg_ap = 0.0

            history["val_loss"].append(val_loss)
            history["val_auc"].append(agg_auc)
            history["val_ap"].append(agg_ap)

            # Print epoch summary
            print(
                f"  Epoch {epoch:>4d}/{config.epochs} | "
                f"train_loss={total_loss.item():.4f} | "
                f"val_loss={val_loss:.4f} | "
                f"val_AUC={agg_auc:.4f} | val_AP={agg_ap:.4f} | "
                f"{elapsed:.1f}s"
            )

            # Per-relation detail
            for triplet, m in val_metrics.items():
                print(f"    {_fmt(triplet)}: {m}")

            # ── Early stopping ─────────────────────────────────────────
            if agg_auc > best_val_auc:
                best_val_auc = agg_auc
                patience_counter = 0
                _save_checkpoint(encoder, predictor, config)
            else:
                patience_counter += 1
                if patience_counter >= config.patience:
                    print(
                        f"\n[training] Early stopping at epoch {epoch} "
                        f"(best val AUC = {best_val_auc:.4f})"
                    )
                    break
        else:
            # Non-validation epoch — just print loss
            if epoch % 10 == 0:
                print(
                    f"  Epoch {epoch:>4d}/{config.epochs} | "
                    f"train_loss={total_loss.item():.4f} | {elapsed:.1f}s"
                )

    print(f"\n[training] Done. Best val AUC = {best_val_auc:.4f}")
    return history


# ── Helpers ────────────────────────────────────────────────────────────

def _fmt(triplet: Tuple[str, str, str]) -> str:
    return f"({triplet[0]}, {triplet[1]}, {triplet[2]})"


def _save_checkpoint(
    encoder: HeteroRGATEncoder,
    predictor: LinkPredictor,
    config: RGATConfig,
) -> None:
    """Save model checkpoint."""
    import os
    os.makedirs(config.checkpoint_dir, exist_ok=True)
    path = os.path.join(config.checkpoint_dir, "best_model.pt")
    torch.save(
        {
            "encoder_state_dict": encoder.state_dict(),
            "predictor_state_dict": predictor.state_dict(),
        },
        path,
    )
    print(f"    [checkpoint] Saved best model to {path}")
