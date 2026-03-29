"""
rgat.config – Centralised configuration for the RGAT training pipeline.

All hyper-parameters, paths, and schema constants live here so that every
other module can import a single ``RGATConfig`` instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple


# ── Expected node types ────────────────────────────────────────────────
VALID_NODE_TYPES: FrozenSet[str] = frozenset({
    "repo", "file", "module", "class", "function",
})

# ── Expected edge types ────────────────────────────────────────────────
VALID_EDGE_TYPES: FrozenSet[str] = frozenset({
    "CONTAINS_FILE",
    "CONTAINS_MODULE",
    "IMPLEMENTS_MODULE",
    "DEFINES_CLASS",
    "DEFINES_FUNCTION",
    "DEFINES_METHOD",
    "IMPORTS_MODULE",
    "INHERITS",
    "CALLS",
})

# ── Required numeric / boolean feature keys per node type ──────────────
# ``embedding_input`` (str) and ``leiden_community`` (int) are handled
# separately and are NOT listed here.
REQUIRED_SCALAR_FEATURES: Dict[str, List[str]] = {
    "repo": [
        "num_files", "num_modules", "num_classes", "num_functions",
        "total_loc", "num_packages",
        "pagerank", "hub_score", "authority_score",
    ],
    "file": [
        "loc", "byte_size", "path_depth", "num_top_level_stmts",
        "is_init", "is_test",
        "pagerank", "hub_score", "authority_score",
    ],
    "module": [
        "num_imports", "num_import_names", "num_classes_defined",
        "num_functions_defined", "num_global_vars",
        "has_docstring", "docstring_length",
        "import_fan_out", "import_fan_in",
        "pagerank", "hub_score", "authority_score",
    ],
    "class": [
        "num_methods", "num_bases", "inheritance_depth", "num_decorators",
        "has_docstring", "docstring_length", "num_class_vars",
        "is_abstract", "is_nested", "num_dunder_methods", "line_span",
        "in_degree", "out_degree",
        "pagerank", "hub_score", "authority_score",
    ],
    "function": [
        "num_params", "has_varargs", "has_kwargs", "num_defaults",
        "has_return_annotation", "type_hint_coverage",
        "is_async", "is_staticmethod", "is_classmethod", "is_property",
        "is_abstractmethod", "is_dunder", "is_init", "is_private",
        "num_decorators", "loc", "body_stmt_count",
        "has_docstring", "docstring_length",
        "num_local_vars", "num_returns", "num_yields", "num_raises",
        "nesting_depth", "cyclomatic_complexity", "num_calls_made",
        "in_degree", "out_degree",
        "pagerank", "hub_score", "authority_score",
    ],
}


@dataclass
class RGATConfig:
    """Central configuration object for the RGAT training pipeline."""

    # ── Paths ──────────────────────────────────────────────────────────
    json_path: str = "django_ecosystem_v1.json"
    cache_dir: str = "cache"
    checkpoint_dir: str = "checkpoints"

    # ── Text encoder ───────────────────────────────────────────────────
    sentence_model: str = "all-MiniLM-L6-v2"
    sentence_dim: int = 384
    text_encode_batch_size: int = 256

    # ── Leiden community embedding ─────────────────────────────────────
    leiden_embed_dim: int = 16

    # ── Model architecture ─────────────────────────────────────────────
    hidden_dim: int = 128
    num_heads: int = 8
    num_layers: int = 2
    dropout: float = 0.2

    # ── Training ───────────────────────────────────────────────────────
    lr: float = 1e-3
    weight_decay: float = 1e-4
    epochs: int = 200
    val_every: int = 5
    patience: int = 25          # early-stopping patience (in val checks)
    diversity_loss_weight: float = 1.0  # weight for attention diversity regulariser (0 = disabled)
    diversity_warmup_epochs: int = 10    # apply diversity loss only after this many epochs

    # ── Link prediction ────────────────────────────────────────────────
    val_ratio: float = 0.15
    neg_sampling_ratio: float = 1.0

    # Edge types supervised for link prediction.
    # Triplets are *inferred* from data; this list controls which
    # ``relation_type`` labels are included in the prediction target.
    supervised_relations: Tuple[str, ...] = (
        "CALLS",
        "INHERITS",
        "IMPORTS_MODULE",
    )

    # ── Device ─────────────────────────────────────────────────────────
    device: str = "cpu"         # overridden to "mps" / "cuda" if available

    # ── Derived (populated at runtime) ─────────────────────────────────
    # Maps node_type → scalar feature dimension (set by graph_construction)
    scalar_dims: Dict[str, int] = field(default_factory=dict)
    # Maps node_type → total input dimension (scalar + sentence + leiden)
    input_dims: Dict[str, int] = field(default_factory=dict)
    # Number of unique Leiden community IDs (including the -1→0 remap)
    num_leiden_ids: int = 0

    def resolve_device(self) -> str:
        """Pick the best available device and store it."""
        import torch

        if torch.cuda.is_available():
            self.device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
        return self.device
