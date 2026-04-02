#!/usr/bin/env python3
"""Full RGAT pipeline run with detailed per-stage timing."""

import time
import os
import sys

# ── Stage timers ───────────────────────────────────────────────────────
timings = {}

def timed(label):
    """Context manager that records wall-clock time for a stage."""
    class Timer:
        def __enter__(self):
            self.t0 = time.perf_counter()
            print(f"\n{'─'*65}")
            print(f"  ▶ {label}")
            print(f"{'─'*65}")
            return self
        def __exit__(self, *args):
            elapsed = time.perf_counter() - self.t0
            timings[label] = elapsed
            print(f"  ✓ {label} — {elapsed:.1f}s")
    return Timer()

# ══════════════════════════════════════════════════════════════════════
t_total_start = time.perf_counter()

import torch
print(f"PyTorch {torch.__version__}  |  Device candidates: ", end="")
if torch.cuda.is_available():
    print(f"CUDA ({torch.cuda.get_device_name(0)})")
elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
    print("MPS (Apple Silicon)")
else:
    print("CPU only")

from rgat.config import RGATConfig

config = RGATConfig(
    json_path="django_ecosystem_v1.json",
    hidden_dim=128,
    num_heads=4,
    num_layers=2,
    dropout=0.2,
    lr=1e-3,
    epochs=100,
    val_every=5,
    patience=15,
    val_ratio=0.15,
    neg_sampling_ratio=1.0,
)
config.resolve_device()
print(f"Selected device: {config.device}\n")

# ── Stage 1: Load JSON ────────────────────────────────────────────────
with timed("1. Load & validate JSON"):
    from rgat.data_loading import load_json
    metadata, nodes, edges = load_json(config.json_path)

# ── Stage 2: Schema validation ────────────────────────────────────────
with timed("2. Schema validation"):
    from rgat.schema_validation import validate_features
    validate_features(nodes)

# ── Stage 3: Text encoding ────────────────────────────────────────────
with timed("3. Sentence-transformer encoding"):
    from rgat.text_encoder import encode_texts
    text_embeddings = encode_texts(
        nodes,
        model_name=config.sentence_model,
        cache_dir=config.cache_dir,
        batch_size=config.text_encode_batch_size,
        json_path=config.json_path,
    )

# ── Stage 4: Build HeteroData ─────────────────────────────────────────
with timed("4. Build HeteroData graph"):
    from rgat.graph_construction import build_hetero_data
    data, node_index = build_hetero_data(nodes, edges, text_embeddings, config)
    del nodes, edges, text_embeddings  # free memory

# ── Stage 5: Edge split ───────────────────────────────────────────────
with timed("5. Edge splitting (train/val)"):
    from rgat.edge_split import split_edges
    train_data, val_data = split_edges(data, config)

# ── Stage 6: Build model ──────────────────────────────────────────────
with timed("6. Build model"):
    from rgat.model import HeteroRGATEncoder, LinkPredictor

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

    n_enc = sum(p.numel() for p in encoder.parameters())
    n_pred = sum(p.numel() for p in predictor.parameters())
    print(f"  Encoder params : {n_enc:,}")
    print(f"  Predictor params: {n_pred:,}")
    print(f"  Total params   : {n_enc + n_pred:,}")

# ── Stage 7: Training ─────────────────────────────────────────────────
with timed("7. Training (masked link prediction)"):
    from rgat.training import train
    history = train(encoder, predictor, train_data, val_data, config)

# ── Stage 8: Final evaluation ─────────────────────────────────────────
with timed("8. Final evaluation on val set"):
    import torch.nn as nn
    from rgat.training import evaluate_link_prediction

    device = torch.device(config.device)
    encoder = encoder.to(device)
    predictor = predictor.to(device)

    supervised_triplets = [
        t for t in train_data.edge_types
        if t[1] in config.supervised_relations
        and hasattr(val_data[t], "edge_label_index")
    ]

    criterion = nn.BCEWithLogitsLoss()
    per_rel, total_loss = evaluate_link_prediction(
        encoder, predictor, val_data.to(device), supervised_triplets, criterion,
    )

    print(f"\n  Final validation loss: {total_loss:.4f}")
    for triplet, m in per_rel.items():
        print(f"    ({triplet[0]}, {triplet[1]}, {triplet[2]}): {m}")

# ── Stage 9: Attention extraction (optional) ──────────────────────────
with timed("9. Attention weight extraction"):
    from rgat.attention import get_attention_weights
    attn_maps = get_attention_weights(encoder, val_data.to(device))
    total_attn_edges = 0
    for layer_idx, amap in enumerate(attn_maps):
        n_types = len(amap)
        n_edges = sum(a[1].shape[0] for a in amap.values())
        total_attn_edges += n_edges
        print(f"  Layer {layer_idx}: {n_types} edge types, {n_edges:,} attention values")
    print(f"  Total attention entries: {total_attn_edges:,}")

# ══════════════════════════════════════════════════════════════════════
t_total = time.perf_counter() - t_total_start

print(f"\n{'═'*65}")
print(f"  TIMING SUMMARY")
print(f"{'═'*65}")
for label, secs in timings.items():
    pct = 100 * secs / t_total
    bar = "█" * int(pct / 2)
    print(f"  {label:<45s} {secs:>7.1f}s  ({pct:>5.1f}%)  {bar}")
print(f"{'─'*65}")
print(f"  {'TOTAL':<45s} {t_total:>7.1f}s")
print(f"{'═'*65}")

# Memory stats
import resource
peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
if sys.platform == "darwin":
    # macOS reports bytes, not kilobytes
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
print(f"\n  Peak memory (RSS): {peak_mb:.0f} MB")
print(f"  Device used: {config.device}")
if config.device == "mps":
    print(f"  MPS allocated: {torch.mps.current_allocated_memory() / 1e6:.0f} MB")
elif config.device == "cuda":
    print(f"  CUDA allocated: {torch.cuda.memory_allocated() / 1e6:.0f} MB")
    print(f"  CUDA peak: {torch.cuda.max_memory_allocated() / 1e6:.0f} MB")
