"""
rgat – Relational Graph Attention Network training pipeline.

Trains a heterogeneous GATv2-based encoder on a code-relationship graph
produced by ``graph_builder``, with masked link prediction as the primary
training objective.

Usage:
    python -m rgat --json django_ecosystem_v1.json
"""

from rgat.config import RGATConfig  # noqa: F401

__all__ = ["RGATConfig"]
