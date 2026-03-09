"""
graph_builder.cli – Command-line interface for the code graph builder.

Usage::

    python -m graph_builder --repo_root /path/to/repo --repo_name my_repo --out graph.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    """Entry-point for the CLI."""
    parser = argparse.ArgumentParser(
        prog="graph_builder",
        description=(
            "Analyse a Python repository and produce a heterogeneous directed "
            "graph (JSON) suitable for R-GAT / GNN workloads."
        ),
    )
    parser.add_argument(
        "--repo_root",
        type=Path,
        required=True,
        help="Path to the repository root directory.",
    )
    parser.add_argument(
        "--repo_name",
        type=str,
        required=True,
        help="Short human-readable name for the repository (used in node IDs).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("graph.json"),
        help="Output JSON file path (default: graph.json).",
    )
    parser.add_argument(
        "--no-features",
        action="store_true",
        default=False,
        help="Skip node feature computation (topology-only output).",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    if not repo_root.is_dir():
        print(f"Error: {repo_root} is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Import here so --help stays fast and doesn't trigger ast parsing
    from graph_builder.builder import GraphBuilder

    builder = GraphBuilder(repo_root=repo_root, repo_name=args.repo_name,
                           compute_features=not args.no_features)
    # Jedi is enabled automatically by GraphBuilder if installed.
    graph = builder.build()
    graph.write_json(args.out)

    # Print summary to stdout
    print(graph.summary())
    print(f"\nGraph written to {args.out}")
