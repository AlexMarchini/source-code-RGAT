"""
rgat.text_encoder – Offline sentence-transformer encoding with disk cache.

Pre-computes 384-dim embeddings for every node's ``embedding_input`` string
using ``all-MiniLM-L6-v2`` and caches them as ``.pt`` files so they only
need to be generated once.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import torch
from torch import Tensor


def encode_texts(
    nodes: List[Dict[str, Any]],
    *,
    model_name: str = "all-MiniLM-L6-v2",
    cache_dir: str = "cache",
    batch_size: int = 256,
    json_path: str = "",
) -> Dict[str, Tensor]:
    """Encode ``embedding_input`` strings for every node, grouped by type.

    Parameters
    ----------
    nodes : list[dict]
        Raw node dicts (already validated).
    model_name : str
        Sentence-transformers model identifier.
    cache_dir : str
        Directory for caching ``.pt`` files.
    batch_size : int
        Encoding batch size.
    json_path : str
        Path to the source JSON — used to build a cache key.

    Returns
    -------
    dict[str, Tensor]
        Mapping ``node_type → Tensor[N_type, embed_dim]``.
        Tensors are ordered consistently with the node order within each type.
    """
    from sentence_transformers import SentenceTransformer

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    # Build a cache key from the JSON path + node count
    cache_key = _cache_key(json_path, len(nodes))

    # Group texts by node type (preserving order).
    # For nodes with empty embedding_input, derive a fallback from the node ID.
    type_texts: Dict[str, List[str]] = {}
    n_fallback = 0
    for node in nodes:
        ntype = node["type"]
        text = node["features"].get("embedding_input", "")
        if not text.strip():
            # Derive from node ID: e.g. "func::repo::mod::qualname" → "mod qualname"
            parts = node["id"].split("::")
            text = " ".join(parts[2:]) if len(parts) > 2 else node["id"]
            n_fallback += 1
        type_texts.setdefault(ntype, []).append(text)

    if n_fallback:
        print(f"[text_encoder] {n_fallback} node(s) used ID-derived fallback text")

    # Check if all cache files exist
    all_cached = all(
        (cache_path / f"{cache_key}_{ntype}.pt").exists()
        for ntype in type_texts
    )

    if all_cached:
        print("[text_encoder] Loading cached embeddings …")
        result: Dict[str, Tensor] = {}
        for ntype in type_texts:
            fpath = cache_path / f"{cache_key}_{ntype}.pt"
            result[ntype] = torch.load(fpath, map_location="cpu", weights_only=True)
            print(f"  {ntype}: {result[ntype].shape}")
        return result

    # Encode fresh
    print(f"[text_encoder] Loading sentence-transformer '{model_name}' …")
    model = SentenceTransformer(model_name)

    result = {}
    for ntype, texts in type_texts.items():
        print(
            f"[text_encoder] Encoding {len(texts):,} {ntype} texts …",
            flush=True,
        )
        t0 = time.perf_counter()
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=True,
            convert_to_tensor=True,
        )
        elapsed = time.perf_counter() - t0
        # sentence-transformers may return on various devices; ensure CPU
        embeddings = embeddings.cpu().float()
        result[ntype] = embeddings
        print(
            f"  {ntype}: {embeddings.shape} in {elapsed:.1f}s "
            f"({len(texts)/elapsed:.0f} texts/s)"
        )
        # Save to cache
        fpath = cache_path / f"{cache_key}_{ntype}.pt"
        torch.save(embeddings, fpath)

    print("[text_encoder] Embeddings cached to disk.")
    return result


def _cache_key(json_path: str, n_nodes: int) -> str:
    """Deterministic short cache key from the JSON path + node count."""
    raw = f"{json_path}::{n_nodes}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
