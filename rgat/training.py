"""
rgat.training – Training loop, evaluation, and metric computation (v2).

Changes from v1:
- Supports RelationDecoder (DistMult/bilinear) as well as legacy LinkPredictor
- Auxiliary supervision: same-repo prediction, degree-bucket prediction
- Diversity metric logging per edge type
- Structured attention collection from all layers
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from torch import Tensor
from torch_geometric.data import HeteroData

from sklearn.metrics import average_precision_score, roc_auc_score

from rgat.config import RGATConfig
from rgat.model import (
    AttentionDiversityLoss,
    DegreeBucketHead,
    HeteroRGATEncoder,
    LinkPredictor,
    RelationDecoder,
    SameRepoHead,
    compute_diversity_metrics,
)


# Type alias for the decoder (supports both old and new)
Decoder = Union[RelationDecoder, LinkPredictor]


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
    predictor: Decoder,
    data: HeteroData,
    supervised_triplets: List[Tuple[str, str, str]],
    criterion: nn.Module,
) -> Tuple[Dict[Tuple[str, str, str], Metrics], float]:
    """Evaluate link prediction on a single data split.

    Parameters
    ----------
    encoder : HeteroRGATEncoder
    predictor : RelationDecoder or LinkPredictor
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
    if hasattr(predictor, "eval"):
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

        logits = _decode(predictor, z_src, z_dst, triplet)
        loss = criterion(logits, edge_label)

        # Detach for metric computation
        probs_np = torch.sigmoid(logits).detach().cpu().numpy()
        logits_np = logits.detach().cpu().numpy()
        labels_np = edge_label.detach().cpu().numpy()
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


# ── Training loop ──────────────────────────────────────────────────────

def train(
    encoder: HeteroRGATEncoder,
    predictor: Decoder,
    train_data: HeteroData,
    val_data: HeteroData,
    config: RGATConfig,
    same_repo_head: Optional[SameRepoHead] = None,
    degree_head: Optional[DegreeBucketHead] = None,
) -> Dict[str, list]:
    """Run the full training loop.

    Parameters
    ----------
    encoder, predictor : nn.Module
    train_data, val_data : HeteroData
    config : RGATConfig
    same_repo_head : SameRepoHead, optional
        Auxiliary head for same-repo prediction.
    degree_head : DegreeBucketHead, optional
        Auxiliary head for degree-bucket prediction.

    Returns
    -------
    history : dict
        Training history with keys ``"epoch"``, ``"train_loss"``,
        ``"val_loss"``, ``"val_auc"``, ``"val_ap"``, ``"diversity_metrics"``,
        and per-relation metrics.
    """
    device = torch.device(config.device)
    encoder = encoder.to(device)
    predictor = predictor.to(device)
    train_data = train_data.to(device)
    val_data = val_data.to(device)

    if same_repo_head is not None:
        same_repo_head = same_repo_head.to(device)
    if degree_head is not None:
        degree_head = degree_head.to(device)

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
    print(f"[training] Decoder type: {config.decoder_type}")
    print(f"[training] Aux same-repo weight: {config.aux_same_repo_weight}")
    print(f"[training] Aux degree weight: {config.aux_degree_weight}")

    # ── Optimizer and loss ─────────────────────────────────────────────
    params = list(encoder.parameters()) + list(predictor.parameters())
    if same_repo_head is not None:
        params += list(same_repo_head.parameters())
    if degree_head is not None:
        params += list(degree_head.parameters())
    optimizer = torch.optim.Adam(params, lr=config.lr, weight_decay=config.weight_decay)
    criterion = nn.BCEWithLogitsLoss()

    # ── Attention diversity regulariser ────────────────────────────────
    use_diversity = config.diversity_loss_weight > 0
    diversity_criterion = None
    if use_diversity:
        diversity_criterion = AttentionDiversityLoss(
            ortho_weight=config.diversity_ortho_weight,
            variance_weight=config.diversity_variance_weight,
            coverage_weight=config.diversity_coverage_weight,
            num_layers=config.num_layers,
        ).to(device)

    # ── History ────────────────────────────────────────────────────────
    history: Dict[str, list] = {
        "epoch": [],
        "train_loss": [],
        "val_loss": [],
        "val_auc": [],
        "val_ap": [],
        "diversity_metrics": [],
    }

    best_val_auc = 0.0
    patience_counter = 0

    # ── Training epochs ────────────────────────────────────────────────
    for epoch in range(1, config.epochs + 1):
        t0 = time.perf_counter()
        encoder.train()
        if hasattr(predictor, "train"):
            predictor.train()
        if same_repo_head is not None:
            same_repo_head.train()
        if degree_head is not None:
            degree_head.train()
        optimizer.zero_grad()

        # Forward — request attention weights when diversity loss is active
        need_attn = use_diversity and epoch > config.diversity_warmup_epochs
        if need_attn:
            z_dict, attn_dict = encoder(train_data, return_attention_weights=True)
        else:
            z_dict = encoder(train_data)
            attn_dict = None

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

            logits = _decode(predictor, z_src, z_dst, triplet)
            loss = criterion(logits, edge_label)
            total_loss = total_loss + loss

        # ── Attention diversity regularisation (gradient-connected) ──
        if need_attn and attn_dict and diversity_criterion is not None:
            diversity_criterion.cached_attn = attn_dict
            div_loss = diversity_criterion(encoder)
            total_loss = total_loss + config.diversity_loss_weight * div_loss

        # ── Auxiliary: same-repo prediction ──────────────────────────
        if same_repo_head is not None and config.aux_same_repo_weight > 0:
            aux_repo_loss = _compute_same_repo_loss(
                same_repo_head, z_dict, train_data, supervised_triplets, device
            )
            if aux_repo_loss is not None:
                total_loss = total_loss + config.aux_same_repo_weight * aux_repo_loss

        # ── Auxiliary: degree-bucket prediction ──────────────────────
        if degree_head is not None and config.aux_degree_weight > 0:
            aux_deg_loss = _compute_degree_loss(
                degree_head, z_dict, train_data, device,
                num_buckets=config.aux_degree_num_buckets,
            )
            if aux_deg_loss is not None:
                total_loss = total_loss + config.aux_degree_weight * aux_deg_loss

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

            # ── Diversity metrics logging ──────────────────────────────
            div_metrics: Dict[str, float] = {}
            if attn_dict:
                div_metrics = compute_diversity_metrics(attn_dict)
                # Print summary stats
                cos_sims = [v for k, v in div_metrics.items() if k.endswith("/cosine_sim")]
                entropies = [v for k, v in div_metrics.items() if k.endswith("/mean_entropy")]
                variances = [v for k, v in div_metrics.items() if k.endswith("/mean_variance")]
                if cos_sims:
                    import numpy as np
                    print(
                        f"    [diversity] cos_sim={np.mean(cos_sims):.4f}  "
                        f"entropy={np.mean(entropies):.4f}  "
                        f"variance={np.mean(variances):.6f}"
                    )
            history["diversity_metrics"].append(div_metrics)

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
                _save_checkpoint(encoder, predictor, config,
                                 same_repo_head=same_repo_head,
                                 degree_head=degree_head)
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


# ── Internal helpers ───────────────────────────────────────────────────

def _decode(
    predictor: Decoder,
    z_src: Tensor,
    z_dst: Tensor,
    triplet: Tuple[str, str, str],
) -> Tensor:
    """Call the decoder, handling both RelationDecoder and LinkPredictor."""
    if isinstance(predictor, RelationDecoder):
        return predictor(z_src, z_dst, triplet)
    return predictor(z_src, z_dst)


def _compute_same_repo_loss(
    head: SameRepoHead,
    z_dict: Dict[str, Tensor],
    data: HeteroData,
    supervised_triplets: List[Tuple[str, str, str]],
    device: torch.device,
) -> Optional[Tensor]:
    """Compute same-repo auxiliary loss across supervised edges."""
    criterion = nn.BCEWithLogitsLoss()
    losses = []

    for triplet in supervised_triplets:
        src_type, rel, dst_type = triplet
        store = data[triplet]
        if not hasattr(store, "same_repo_label"):
            continue

        # Use edge_index (message-passing edges), NOT edge_label_index
        # which includes negatives and doesn't align with same_repo_label.
        edge_index = store.edge_index
        same_repo = store.same_repo_label.float()

        if same_repo.numel() == 0:
            continue

        z_src = z_dict[src_type][edge_index[0]]
        z_dst = z_dict[dst_type][edge_index[1]]
        logits = head(z_src, z_dst)
        losses.append(criterion(logits, same_repo))

    if not losses:
        return None
    return torch.stack(losses).mean()


def _compute_degree_loss(
    head: DegreeBucketHead,
    z_dict: Dict[str, Tensor],
    data: HeteroData,
    device: torch.device,
    num_buckets: int = 6,
) -> Optional[Tensor]:
    """Compute degree-bucket auxiliary loss on node types with degree info."""
    criterion = nn.CrossEntropyLoss()
    losses = []

    for ntype, z in z_dict.items():
        if not hasattr(data[ntype], "degree_bucket"):
            continue
        labels = data[ntype].degree_bucket
        if labels.numel() == 0:
            continue
        logits = head(z)
        losses.append(criterion(logits, labels))

    if not losses:
        return None
    return torch.stack(losses).mean()


def _fmt(triplet: Tuple[str, str, str]) -> str:
    return f"({triplet[0]}, {triplet[1]}, {triplet[2]})"


def _save_checkpoint(
    encoder: HeteroRGATEncoder,
    predictor: Decoder,
    config: RGATConfig,
    same_repo_head: Optional[SameRepoHead] = None,
    degree_head: Optional[DegreeBucketHead] = None,
) -> None:
    """Save model checkpoint."""
    import os
    os.makedirs(config.checkpoint_dir, exist_ok=True)
    path = os.path.join(config.checkpoint_dir, "best_model.pt")
    ckpt = {
        "encoder_state_dict": encoder.state_dict(),
        "predictor_state_dict": predictor.state_dict(),
    }
    if same_repo_head is not None:
        ckpt["same_repo_head_state_dict"] = same_repo_head.state_dict()
    if degree_head is not None:
        ckpt["degree_head_state_dict"] = degree_head.state_dict()
    torch.save(ckpt, path)
    print(f"    [checkpoint] Saved best model to {path}")
