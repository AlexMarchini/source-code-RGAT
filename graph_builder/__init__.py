"""
graph_builder – Python Code Graph Builder

Analyses any Python repository using the `ast` module and constructs a
heterogeneous directed graph suitable for R-GAT and similar GNN workloads.

Usage (programmatic):
    from graph_builder import GraphBuilder
    graph = GraphBuilder(repo_root, repo_name).build()
    graph.write_json("graph.json")

Usage (CLI):
    python -m graph_builder --repo_root /path/to/repo --repo_name my_repo --out graph.json
"""

from graph_builder.model import Node, Edge, Graph          # noqa: F401
from graph_builder.builder import GraphBuilder              # noqa: F401
from graph_builder.git_enricher import enrich_git            # noqa: F401

__all__ = ["Node", "Edge", "Graph", "GraphBuilder", "enrich_git"]
