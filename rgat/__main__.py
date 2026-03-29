"""
rgat.__main__ – CLI entry point: ``python -m rgat``.

Orchestrates the full pipeline:
  1. Load & validate JSON
  2. Encode text features (with caching)
  3. Build HeteroData graph
  4. Split supervised edges
  5. Build model
  6. Train with masked link prediction
  7. Report final metrics
"""

from __future__ import annotations

import argparse
import sys
import time

import torch

from rgat.config import RGATConfig
from rgat.data_loading import load_json
from rgat.schema_validation import validate_features
from rgat.data_cleaning import clean_graph
from rgat.text_encoder import encode_texts
from rgat.graph_construction import build_hetero_data
from rgat.edge_split import split_edges
from rgat.model import HeteroRGATEncoder, LinkPredictor
from rgat.training import train


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train an RGAT model on a heterogeneous code graph.",
    )
    parser.add_argument(
        "--json", type=str, default="django_ecosystem_v1.json",
        help="Path to the graph JSON file.",
    )
    parser.add_argument("--hidden-dim", type=int, default=128)
    parser.add_argument("--num-heads", type=int, default=4)
    parser.add_argument("--num-layers", type=int, default=2)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--val-every", type=int, default=5)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--neg-ratio", type=float, default=1.0)
    parser.add_argument("--leiden-embed-dim", type=int, default=16)
    parser.add_argument(
        "--sentence-model", type=str, default="all-MiniLM-L6-v2",
    )
    parser.add_argument("--cache-dir", type=str, default="cache")
    parser.add_argument("--checkpoint-dir", type=str, default="checkpoints")
    parser.add_argument(
        "--device", type=str, default="auto",
        help="Device: 'cpu', 'cuda', 'mps', or 'auto'.",
    )
    args = parser.parse_args()

    # ── Build config ───────────────────────────────────────────────────
    config = RGATConfig(
        json_path=args.json,
        cache_dir=args.cache_dir,
        checkpoint_dir=args.checkpoint_dir,
        sentence_model=args.sentence_model,
        leiden_embed_dim=args.leiden_embed_dim,
        hidden_dim=args.hidden_dim,
        num_heads=args.num_heads,
        num_layers=args.num_layers,
        dropout=args.dropout,
        lr=args.lr,
        epochs=args.epochs,
        val_every=args.val_every,
        patience=args.patience,
        val_ratio=args.val_ratio,
        neg_sampling_ratio=args.neg_ratio,
    )

    if args.device == "auto":
        config.resolve_device()
    else:
        config.device = args.device

    print(f"\n{'='*65}")
    print(f"  RGAT TRAINING PIPELINE")
    print(f"{'='*65}")
    print(f"  JSON    : {config.json_path}")
    print(f"  Device  : {config.device}")
    print(f"  Hidden  : {config.hidden_dim}  Heads: {config.num_heads}  "
          f"Layers: {config.num_layers}")
    print(f"  LR      : {config.lr}  Epochs: {config.epochs}  "
          f"Patience: {config.patience}")
    print(f"  Val     : {config.val_ratio:.0%}  Neg ratio: {config.neg_sampling_ratio}")
    print(f"  Supervised: {config.supervised_relations}")
    print(f"{'='*65}\n")

    t_start = time.perf_counter()

    # ── Step 1: Load & validate ────────────────────────────────────────
    metadata, nodes, edges = load_json(config.json_path)
    validate_features(nodes)

    # ── Step 1b: Data quality cleaning ─────────────────────────────────
    nodes, edges = clean_graph(nodes, edges)

    # ── Step 2: Encode text features ───────────────────────────────────
    text_embeddings = encode_texts(
        nodes,
        model_name=config.sentence_model,
        cache_dir=config.cache_dir,
        batch_size=config.text_encode_batch_size,
        json_path=config.json_path,
    )

    # ── Step 3: Build HeteroData ───────────────────────────────────────
    data, node_index = build_hetero_data(nodes, edges, text_embeddings, config)

    # Free raw data
    del nodes, edges, text_embeddings

    # ── Step 4: Split edges ────────────────────────────────────────────
    train_data, val_data = split_edges(data, config)

    # ── Step 5: Build model ────────────────────────────────────────────
    # Collect all edge types from train_data (includes reverse edges)
    all_edge_types = list(train_data.edge_types)
    all_node_types = list(config.input_dims.keys())

    encoder = HeteroRGATEncoder(
        node_types=all_node_types,
        edge_types=all_edge_types,
        scalar_dims=config.scalar_dims,
        sentence_dim=config.sentence_dim,
        leiden_embed_dim=config.leiden_embed_dim,
        num_leiden_ids=config.num_leiden_ids,
        hidden_dim=config.hidden_dim,
        num_heads=config.num_heads,
        num_layers=config.num_layers,
        dropout=config.dropout,
    )
    predictor = LinkPredictor()

    # Print model summary
    n_params_enc = sum(p.numel() for p in encoder.parameters())
    n_params_pred = sum(p.numel() for p in predictor.parameters())
    print(f"[model] Encoder parameters : {n_params_enc:,}")
    print(f"[model] Predictor parameters: {n_params_pred:,}")
    print(f"[model] Total parameters   : {n_params_enc + n_params_pred:,}")

    # ── Step 6: Train ──────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  TRAINING")
    print("=" * 65)

    history = train(encoder, predictor, train_data, val_data, config)

    elapsed_total = time.perf_counter() - t_start
    print(f"\n[pipeline] Total runtime: {elapsed_total:.0f}s")

    # ── Step 7: Save node index for downstream use ─────────────────────
    import os, json
    os.makedirs(config.cache_dir, exist_ok=True)
    idx_path = os.path.join(config.cache_dir, "node_index.json")
    with open(idx_path, "w") as f:
        json.dump(node_index, f)
    print(f"[pipeline] Node index saved to {idx_path}")
    print("[pipeline] Checkpoints in", config.checkpoint_dir)
    print("[pipeline] Done.")


if __name__ == "__main__":
    main()
