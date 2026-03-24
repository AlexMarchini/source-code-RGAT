"""
graph_builder.cli – Command-line interface for the code graph builder.

Usage (single repo)::

    python -m graph_builder --repo_root /path/to/repo --repo_name my_repo --out graph.json

Usage (multi-repo)::

    python -m graph_builder \\
        --repo django:/path/to/django \\
        --repo drf:/path/to/drf \\
        --out graph.json
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
            "Analyse one or more Python repositories and produce a "
            "heterogeneous directed graph (JSON) suitable for R-GAT / GNN "
            "workloads."
        ),
    )

    # ---- multi-repo interface (preferred) ----
    parser.add_argument(
        "--repo",
        type=str,
        action="append",
        metavar="NAME:PATH",
        default=None,
        help=(
            "Repository in NAME:PATH format.  Can be repeated for multi-repo "
            "builds (e.g. --repo django:/src/django --repo drf:/src/drf)."
        ),
    )

    # ---- legacy single-repo interface ----
    parser.add_argument(
        "--repo_root",
        type=Path,
        default=None,
        help="(Legacy) Path to a single repository root directory.",
    )
    parser.add_argument(
        "--repo_name",
        type=str,
        default=None,
        help="(Legacy) Short human-readable name for the repository.",
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

    # ---- git enrichment options ----
    parser.add_argument(
        "--enrich-git",
        action="store_true",
        default=False,
        help="Enrich the graph with git history data (commits, blame, co-change).",
    )
    parser.add_argument(
        "--git-depth",
        type=int,
        default=500,
        metavar="N",
        help="Max commits per repo for git enrichment (default: 500).",
    )
    parser.add_argument(
        "--git-since",
        type=str,
        default=None,
        metavar="DATE",
        help='Only consider commits after DATE (e.g. "2024-01-01").',
    )
    parser.add_argument(
        "--co-change-min",
        type=int,
        default=3,
        metavar="N",
        help="Min co-occurrence count for CO_CHANGES_WITH edges (default: 3).",
    )
    parser.add_argument(
        "--no-git-blame",
        action="store_true",
        default=False,
        help="Skip git blame computation during enrichment.",
    )
    parser.add_argument(
        "--git-workers",
        type=int,
        default=8,
        metavar="N",
        help="Thread pool size for parallel git operations (default: 8).",
    )
    args = parser.parse_args(argv)

    # ---- resolve repos list ----
    repos = []

    if args.repo:
        for spec in args.repo:
            if ":" not in spec:
                print(
                    f"Error: --repo must be NAME:PATH, got: {spec!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            name, path_str = spec.split(":", 1)
            root = Path(path_str).resolve()
            if not root.is_dir():
                print(f"Error: {root} is not a directory.", file=sys.stderr)
                sys.exit(1)
            repos.append((root, name))

    if not repos:
        # Fall back to legacy single-repo arguments
        if args.repo_root is None or args.repo_name is None:
            print(
                "Error: provide either --repo NAME:PATH (repeatable) "
                "or both --repo_root and --repo_name.",
                file=sys.stderr,
            )
            sys.exit(1)
        repo_root: Path = args.repo_root.resolve()
        if not repo_root.is_dir():
            print(f"Error: {repo_root} is not a directory.", file=sys.stderr)
            sys.exit(1)
        repos.append((repo_root, args.repo_name))

    # Import here so --help stays fast and doesn't trigger ast parsing
    from graph_builder.builder import GraphBuilder

    builder = GraphBuilder(repos=repos, compute_features=not args.no_features)
    # Jedi is enabled automatically by GraphBuilder if installed.
    graph = builder.build()

    # ---- optional git enrichment ----
    if args.enrich_git:
        from graph_builder.git_enricher import enrich_git

        repo_map = {name: root for root, name in repos}
        graph = enrich_git(
            graph,
            repo_map,
            builder=builder,
            depth=args.git_depth,
            since=args.git_since,
            co_change_min=args.co_change_min,
            blame_functions=not args.no_git_blame,
            workers=args.git_workers,
        )

    graph.write_json(args.out)

    # Print summary to stdout
    print(graph.summary())
    print(f"\nGraph written to {args.out}")
