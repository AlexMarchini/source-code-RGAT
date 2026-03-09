"""
graph_builder.features – Node feature computation for GNN / R-GAT training.

Computes numeric and boolean features for every node type in the graph.
Features are written directly into each ``Node.features`` dict and serialised
alongside the topology when the graph is saved to JSON.

Public API
----------
``compute_all_features(graph, builder)``
    Call **after** the graph is fully constructed and pruned.  Populates
    ``node.features`` for every surviving node.
"""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Union

import igraph as ig
import leidenalg

if TYPE_CHECKING:
    from graph_builder.builder import GraphBuilder
    from graph_builder.model import Graph, Node

# ------------------------------------------------------------------ #
# AST metric helpers                                                    #
# ------------------------------------------------------------------ #

# Nodes that contribute +1 to McCabe cyclomatic complexity.
_COMPLEXITY_NODE_TYPES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.Assert,
)

# Nodes that introduce a new nesting level for control flow.
_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)

# Python 3.11+ introduced ast.TryStar
if hasattr(ast, "TryStar"):
    _NESTING_NODES = (*_NESTING_NODES, ast.TryStar)

_DUNDER_RE = re.compile(r"^__\w+__$")

# Maximum whitespace-delimited tokens kept for function embedding_input.
_MAX_FUNC_TOKENS = 768


def cyclomatic_complexity(node: ast.AST) -> int:
    """Compute McCabe cyclomatic complexity for a function / class body.

    Counts ``if | elif | for | while | except | with | assert`` as decision
    points plus ``and`` / ``or`` boolean operators, starting from 1.
    """
    cc = 1
    for child in ast.walk(node):
        if isinstance(child, _COMPLEXITY_NODE_TYPES):
            cc += 1
        elif isinstance(child, ast.BoolOp):
            # ``a and b and c`` → 2 operators (len(values) - 1)
            cc += len(child.values) - 1
    return cc


def max_nesting_depth(body: list[ast.stmt]) -> int:
    """Return the maximum control-flow nesting depth inside *body*.

    Top-level statements are depth 0.
    """

    def _walk(stmts: list[ast.stmt], depth: int) -> int:
        best = depth
        for stmt in stmts:
            if isinstance(stmt, _NESTING_NODES):
                child_depth = depth + 1
                # Recurse into all sub-bodies of this statement
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(stmt, attr, None)
                    if isinstance(sub, list):
                        best = max(best, _walk(sub, child_depth))
            else:
                # Still recurse into bodies that don't add nesting
                # (e.g. function-level statements)
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(stmt, attr, None)
                    if isinstance(sub, list):
                        best = max(best, _walk(sub, depth))
        return best

    return _walk(body, 0)


def count_local_vars(func_node: ast.AST) -> int:
    """Count unique local variable names (``Name`` with ``Store`` context)."""
    names: set[str] = set()
    for child in ast.walk(func_node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
    return len(names)


def count_returns(func_node: ast.AST) -> int:
    return sum(1 for n in ast.walk(func_node) if isinstance(n, ast.Return))


def count_yields(func_node: ast.AST) -> int:
    return sum(
        1 for n in ast.walk(func_node)
        if isinstance(n, (ast.Yield, ast.YieldFrom))
    )


def count_raises(func_node: ast.AST) -> int:
    return sum(1 for n in ast.walk(func_node) if isinstance(n, ast.Raise))


def count_calls(func_node: ast.AST) -> int:
    """Count ``ast.Call`` sites inside a function body."""
    return sum(1 for n in ast.walk(func_node) if isinstance(n, ast.Call))


def _decorator_names(decorator_list: list[ast.expr]) -> list[str]:
    """Extract simple names from a decorator list."""
    names: list[str] = []
    for dec in decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
        elif isinstance(dec, ast.Call):
            # e.g. @decorator(args)
            if isinstance(dec.func, ast.Name):
                names.append(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.append(dec.func.attr)
    return names


def _has_docstring(node: ast.AST) -> bool:
    """Return True if *node* (Module / ClassDef / FunctionDef) has a docstring."""
    return ast.get_docstring(node) is not None


def _docstring_length(node: ast.AST) -> int:
    """Return character length of docstring, 0 if absent."""
    ds = ast.get_docstring(node)
    return len(ds) if ds else 0


def _body_stmt_count(body: list[ast.stmt]) -> int:
    """Recursively count statements in *body*."""
    total = 0
    for stmt in body:
        total += 1
        for attr in ("body", "orelse", "finalbody", "handlers"):
            sub = getattr(stmt, attr, None)
            if isinstance(sub, list):
                total += _body_stmt_count(sub)
    return total


# ------------------------------------------------------------------ #
# Embedding-input helpers                                               #
# ------------------------------------------------------------------ #

def _top_level_names(tree: ast.Module) -> list[str]:
    """Names of all top-level definitions / assignments in a module."""
    names: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.append(stmt.target.id)
    return names


def _class_member_names(cls_node: ast.ClassDef) -> list[str]:
    """Names of methods, class attributes, and annotated attributes."""
    names: list[str] = []
    for stmt in cls_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.append(stmt.target.id)
    return names


def _truncate_tokens(text: str, max_tokens: int) -> str:
    """Return the first *max_tokens* whitespace-delimited tokens of *text*."""
    tokens = text.split()
    return " ".join(tokens[:max_tokens]) if len(tokens) > max_tokens else text.strip()


def _get_function_source(node_id: str, ast_node: ast.AST, builder: "GraphBuilder") -> str:
    """Return the raw source lines for a function AST node."""
    id_parts = node_id.split("::", 3)
    module_name = id_parts[2] if len(id_parts) >= 3 else ""
    module_nid = builder._module_by_name.get(module_name)
    file_nid = builder._file_by_module.get(module_nid, "") if module_nid else ""
    file_parts = file_nid.split("::", 2) if file_nid else []
    relpath = file_parts[2] if len(file_parts) == 3 else ""

    source = builder._source_cache.get(relpath, "")
    if not source or not hasattr(ast_node, "lineno"):
        return ""

    lines = source.splitlines()
    start = ast_node.lineno - 1
    end = getattr(ast_node, "end_lineno", None) or len(lines)
    return "\n".join(lines[start:end])


def _module_embedding_input(module_name: str, tree: Optional[ast.Module]) -> str:
    """module name + names of classes defined in the module."""
    if not tree:
        return module_name
    class_names = [s.name for s in tree.body if isinstance(s, ast.ClassDef)]
    return (module_name + " | " + " ".join(class_names)) if class_names else module_name


def _class_embedding_input(qualname: str, cls_node: Optional[ast.ClassDef]) -> str:
    """class qualname + names of its members."""
    if not cls_node:
        return qualname
    members = _class_member_names(cls_node)
    return (qualname + " | " + " ".join(members)) if members else qualname


# ------------------------------------------------------------------ #
# Per-type feature functions                                            #
# ------------------------------------------------------------------ #

def _compute_repo_features(
    node: Node,
    graph: Graph,
    builder: GraphBuilder,
) -> None:
    """Populate features for the single ``repo`` node."""
    from graph_builder.builder import (
        NT_CLASS,
        NT_FILE,
        NT_FUNCTION,
        NT_MODULE,
    )

    type_counts = Counter(n.type for n in graph.nodes)
    total_loc = 0
    for _relpath, source in builder._source_cache.items():
        total_loc += source.count("\n") + (1 if source and not source.endswith("\n") else 0)

    node.features.update({
        "num_files": type_counts.get(NT_FILE, 0),
        "num_modules": type_counts.get(NT_MODULE, 0),
        "num_classes": type_counts.get(NT_CLASS, 0),
        "num_functions": type_counts.get(NT_FUNCTION, 0),
        "total_loc": total_loc,
        "num_packages": len(builder._packages),
        "embedding_input": builder.repo_name,
    })


def _compute_file_features(
    node: Node,
    builder: GraphBuilder,
) -> None:
    """Populate features for a ``file`` node."""
    # Extract relpath from node id:  file::<repo>::<relpath>
    parts = node.id.split("::", 2)
    relpath = parts[2] if len(parts) == 3 else ""

    source = builder._source_cache.get(relpath, "")
    loc = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    byte_size = len(source.encode("utf-8"))
    path_depth = len(Path(relpath).parts)
    is_init = Path(relpath).name == "__init__.py"
    is_test = "test" in relpath.lower()

    tree = builder._ast_cache.get(relpath)
    num_top_level_stmts = len(tree.body) if tree else 0

    # embedding_input: file path + space-separated top-level definition names
    top_names = _top_level_names(tree) if tree else []
    embedding_input = relpath + (" | " + " ".join(top_names) if top_names else "")

    node.features.update({
        "loc": loc,
        "byte_size": byte_size,
        "path_depth": path_depth,
        "is_init": is_init,
        "is_test": is_test,
        "num_top_level_stmts": num_top_level_stmts,
        "embedding_input": embedding_input,
    })


def _compute_module_features(
    node: Node,
    builder: GraphBuilder,
    in_degree: Dict[str, int],
    out_degree: Dict[str, int],
) -> None:
    """Populate features for a ``module`` node."""
    from graph_builder.builder import ET_IMPORTS_MODULE

    # Derive module name from node id: mod::<repo>::<module_name>
    parts = node.id.split("::", 2)
    module_name = parts[2] if len(parts) == 3 else ""

    # Find the source file for this module
    file_nid = builder._file_by_module.get(node.id, "")
    file_parts = file_nid.split("::", 2) if file_nid else []
    relpath = file_parts[2] if len(file_parts) == 3 else ""

    tree = builder._ast_cache.get(relpath)

    num_imports = 0
    num_import_names = 0
    num_global_vars = 0
    num_classes_defined = 0
    num_functions_defined = 0

    if tree:
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                num_imports += 1
                if hasattr(stmt, "names"):
                    num_import_names += len(stmt.names)
            elif isinstance(stmt, ast.ClassDef):
                num_classes_defined += 1
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                num_functions_defined += 1
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                num_global_vars += 1

    has_ds = _has_docstring(tree) if tree else False
    ds_len = _docstring_length(tree) if tree else 0

    # Import fan-out = IMPORTS_MODULE edges originating from this node
    # Import fan-in  = IMPORTS_MODULE edges targeting this node
    import_fan_out = 0
    import_fan_in = 0
    for e in builder.graph.edges:
        if e.type == ET_IMPORTS_MODULE:
            if e.source == node.id:
                import_fan_out += 1
            if e.target == node.id:
                import_fan_in += 1

    node.features.update({
        "num_imports": num_imports,
        "num_import_names": num_import_names,
        "num_classes_defined": num_classes_defined,
        "num_functions_defined": num_functions_defined,
        "num_global_vars": num_global_vars,
        "has_docstring": has_ds,
        "docstring_length": ds_len,
        "import_fan_out": import_fan_out,
        "import_fan_in": import_fan_in,
        "embedding_input": _module_embedding_input(module_name, tree),
    })


def _compute_class_features(
    node: Node,
    builder: GraphBuilder,
    in_degree: Dict[str, int],
    out_degree: Dict[str, int],
) -> None:
    """Populate features for a ``class`` node."""
    from graph_builder.builder import ET_DEFINES_METHOD, ET_INHERITS

    ast_node: Optional[ast.ClassDef] = builder._ast_nodes.get(node.id)

    # Counts from the AST node
    num_methods = 0
    num_bases = 0
    num_decorators = 0
    has_ds = False
    ds_len = 0
    num_class_vars = 0
    is_nested = False
    num_dunder_methods = 0
    line_span = 0

    if ast_node is not None:
        num_bases = len(ast_node.bases)
        num_decorators = len(ast_node.decorator_list)
        has_ds = _has_docstring(ast_node)
        ds_len = _docstring_length(ast_node)
        line_span = (
            (ast_node.end_lineno - ast_node.lineno + 1)
            if hasattr(ast_node, "end_lineno") and ast_node.end_lineno
            else 0
        )

        for stmt in ast_node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                num_methods += 1
                if _DUNDER_RE.match(stmt.name):
                    num_dunder_methods += 1
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                num_class_vars += 1

    # is_nested: check if the qualname has a dot (e.g. "Outer.Inner")
    # node id: class::<repo>::<module>::<qualname>
    id_parts = node.id.split("::", 3)
    qualname = id_parts[3] if len(id_parts) == 4 else ""
    is_nested = "." in qualname

    # is_abstract: check for ABC or ABCMeta in bases, or @abstractmethod in methods
    is_abstract = False
    if ast_node is not None:
        dec_names = _decorator_names(ast_node.decorator_list)
        # Check base names
        for base in ast_node.bases:
            if isinstance(base, ast.Name) and base.id in ("ABC", "ABCMeta"):
                is_abstract = True
            elif isinstance(base, ast.Attribute) and base.attr in ("ABC", "ABCMeta"):
                is_abstract = True
        # Check for @abstractmethod on any method
        if not is_abstract:
            for stmt in ast_node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_decs = _decorator_names(stmt.decorator_list)
                    if "abstractmethod" in method_decs:
                        is_abstract = True
                        break

    # Inheritance depth via BFS on _class_parents
    inheritance_depth = _compute_inheritance_depth(node.id, builder)

    node.features.update({
        "num_methods": num_methods,
        "num_bases": num_bases,
        "inheritance_depth": inheritance_depth,
        "num_decorators": num_decorators,
        "has_docstring": has_ds,
        "docstring_length": ds_len,
        "num_class_vars": num_class_vars,
        "is_abstract": is_abstract,
        "is_nested": is_nested,
        "num_dunder_methods": num_dunder_methods,
        "line_span": line_span,
        "in_degree": in_degree.get(node.id, 0),
        "out_degree": out_degree.get(node.id, 0),
        "embedding_input": _class_embedding_input(qualname, ast_node),
    })


def _compute_function_features(
    node: Node,
    builder: GraphBuilder,
    in_degree: Dict[str, int],
    out_degree: Dict[str, int],
) -> None:
    """Populate features for a ``function`` node."""
    ast_node: Optional[ast.AST] = builder._ast_nodes.get(node.id)

    # Defaults for when AST node is unavailable
    feats: Dict[str, Union[int, float, bool]] = {
        "num_params": 0,
        "has_varargs": False,
        "has_kwargs": False,
        "num_defaults": 0,
        "has_return_annotation": False,
        "type_hint_coverage": 0.0,
        "is_async": False,
        "is_staticmethod": False,
        "is_classmethod": False,
        "is_property": False,
        "is_abstractmethod": False,
        "is_dunder": False,
        "is_init": False,
        "is_private": False,
        "num_decorators": 0,
        "loc": 0,
        "body_stmt_count": 0,
        "has_docstring": False,
        "docstring_length": 0,
        "num_local_vars": 0,
        "num_returns": 0,
        "num_yields": 0,
        "num_raises": 0,
        "nesting_depth": 0,
        "cyclomatic_complexity": 1,
        "num_calls_made": 0,
        "in_degree": in_degree.get(node.id, 0),
        "out_degree": out_degree.get(node.id, 0),
    }

    if ast_node is not None and isinstance(
        ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        args = ast_node.args

        # Parameter count (exclude self/cls)
        all_params = args.args + args.posonlyargs + args.kwonlyargs
        param_names = [a.arg for a in all_params]
        non_self_params = [p for p in param_names if p not in ("self", "cls")]
        num_params = len(non_self_params)

        # Type hint coverage
        annotated = sum(
            1 for a in all_params
            if a.arg not in ("self", "cls") and a.annotation is not None
        )
        thc = annotated / num_params if num_params > 0 else 0.0

        # Decorator analysis
        dec_names = _decorator_names(ast_node.decorator_list)

        # Extract function name from qualname  (could be Class.method)
        id_parts = node.id.split("::", 3)
        qualname = id_parts[3] if len(id_parts) == 4 else ""
        func_name = qualname.rsplit(".", 1)[-1] if qualname else ""

        # Embedding input: first _MAX_FUNC_TOKENS tokens of raw source
        func_source = _get_function_source(node.id, ast_node, builder)
        emb_input = _truncate_tokens(func_source, _MAX_FUNC_TOKENS)

        feats.update({
            "num_params": num_params,
            "has_varargs": args.vararg is not None,
            "has_kwargs": args.kwarg is not None,
            "num_defaults": len(args.defaults) + len(args.kw_defaults),
            "has_return_annotation": ast_node.returns is not None,
            "type_hint_coverage": round(thc, 4),
            "is_async": isinstance(ast_node, ast.AsyncFunctionDef),
            "is_staticmethod": "staticmethod" in dec_names,
            "is_classmethod": "classmethod" in dec_names,
            "is_property": "property" in dec_names,
            "is_abstractmethod": "abstractmethod" in dec_names,
            "is_dunder": bool(_DUNDER_RE.match(func_name)),
            "is_init": func_name == "__init__",
            "is_private": func_name.startswith("_") and not _DUNDER_RE.match(func_name),
            "num_decorators": len(ast_node.decorator_list),
            "loc": (
                (ast_node.end_lineno - ast_node.lineno + 1)
                if hasattr(ast_node, "end_lineno") and ast_node.end_lineno
                else 0
            ),
            "body_stmt_count": _body_stmt_count(ast_node.body),
            "has_docstring": _has_docstring(ast_node),
            "docstring_length": _docstring_length(ast_node),
            "num_local_vars": count_local_vars(ast_node),
            "num_returns": count_returns(ast_node),
            "num_yields": count_yields(ast_node),
            "num_raises": count_raises(ast_node),
            "nesting_depth": max_nesting_depth(ast_node.body),
            "cyclomatic_complexity": cyclomatic_complexity(ast_node),
            "num_calls_made": count_calls(ast_node),
            "embedding_input": emb_input,
        })

    node.features.update(feats)


# ------------------------------------------------------------------ #
# Inheritance depth helper                                              #
# ------------------------------------------------------------------ #

def _compute_inheritance_depth(class_nid: str, builder: GraphBuilder) -> int:
    """BFS up the ``_class_parents`` chain to compute max inheritance depth."""
    depth = 0
    frontier = [class_nid]
    visited: set[str] = {class_nid}
    while frontier:
        next_frontier: list[str] = []
        for nid in frontier:
            for parent_nid in builder._class_parents.get(nid, []):
                if parent_nid not in visited:
                    visited.add(parent_nid)
                    next_frontier.append(parent_nid)
        if next_frontier:
            depth += 1
        frontier = next_frontier
    return depth


# ------------------------------------------------------------------ #
# Degree computation                                                    #
# ------------------------------------------------------------------ #

def _compute_degrees(graph: Graph) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Compute in-degree and out-degree for every node from the edge list."""
    in_deg: Dict[str, int] = defaultdict(int)
    out_deg: Dict[str, int] = defaultdict(int)
    for e in graph.edges:
        out_deg[e.source] += 1
        in_deg[e.target] += 1
    return dict(in_deg), dict(out_deg)


# ------------------------------------------------------------------ #
# Graph-level metrics (PageRank, HITS, Leiden)                          #
# ------------------------------------------------------------------ #

# Only semantic (code-relationship) edges are used to build the
# analysis subgraph.  Structural / hierarchy edges such as
# CONTAINS_FILE, IMPLEMENTS_MODULE, DEFINES_* are excluded so that
# the metrics reflect code dependency importance, not filesystem layout.
_SEMANTIC_EDGE_TYPES: frozenset[str] = frozenset({
    "IMPORTS_MODULE",
    "INHERITS",
    "CALLS",
})


def _compute_graph_metrics(
    graph: Graph,
) -> Dict[str, Dict[str, Union[float, int]]]:
    """Compute PageRank, HITS (hub/authority), and Leiden community for all nodes.

    Returns a mapping ``{node_id: {metric_name: value, ...}}``.
    Builds a **directed** igraph graph from the semantic edge subset
    (IMPORTS_MODULE, INHERITS, CALLS).  Leiden community detection is
    performed on an undirected (symmetrised) copy.
    """
    # --- build node-id ↔ integer-index mapping ---
    node_ids: List[str] = [n.id for n in graph.nodes]
    id_to_idx: Dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}
    n_nodes = len(node_ids)

    # --- filter semantic edges and map to integer pairs ---
    edge_tuples: List[Tuple[int, int]] = []
    for e in graph.edges:
        if e.type in _SEMANTIC_EDGE_TYPES:
            src = id_to_idx.get(e.source)
            tgt = id_to_idx.get(e.target)
            if src is not None and tgt is not None:
                edge_tuples.append((src, tgt))

    # --- construct directed igraph Graph (all nodes, semantic edges) ---
    g = ig.Graph(n=n_nodes, edges=edge_tuples, directed=True)

    # --- PageRank (directed) ---
    pagerank_scores: List[float] = g.pagerank(directed=True)

    # --- HITS hub & authority scores (directed) ---
    hub_scores: List[float] = g.hub_score()
    authority_scores: List[float] = g.authority_score()

    # --- Leiden communities: connected subgraph only ---
    # Nodes with no semantic edges are isolated in the undirected projection
    # and would each form a trivial singleton community, massively inflating
    # the community count.  We run Leiden on the subgraph induced by nodes
    # that appear in at least one semantic edge, and assign sentinel -1 to
    # all isolates so downstream code can distinguish them.
    connected_indices: List[int] = sorted(
        {idx for pair in edge_tuples for idx in pair}
    )
    idx_to_sub: Dict[int, int] = {
        orig: new for new, orig in enumerate(connected_indices)
    }

    community_ids: List[int] = [-1] * n_nodes
    if connected_indices:
        sub_edges = [
            (idx_to_sub[s], idx_to_sub[t]) for s, t in edge_tuples
        ]
        g_sub = ig.Graph(
            n=len(connected_indices),
            edges=sub_edges,
            directed=False,
        )
        partition = leidenalg.find_partition(
            g_sub,
            leidenalg.ModularityVertexPartition,
        )
        sub_membership: List[int] = list(partition.membership)
        for new_idx, orig_idx in enumerate(connected_indices):
            community_ids[orig_idx] = sub_membership[new_idx]

    # --- assemble per-node metric dicts ---
    metrics: Dict[str, Dict[str, Union[float, int]]] = {}
    for i, nid in enumerate(node_ids):
        metrics[nid] = {
            "pagerank": pagerank_scores[i],
            "hub_score": hub_scores[i],
            "authority_score": authority_scores[i],
            "leiden_community": community_ids[i],
        }
    return metrics


# ------------------------------------------------------------------ #
# Orchestrator                                                          #
# ------------------------------------------------------------------ #

def compute_all_features(graph: Graph, builder: GraphBuilder) -> None:
    """Compute and attach features to every node in *graph*.

    Must be called **after** the graph is fully constructed and pruned so
    that degree counts and edge-based features reflect the final topology.
    """
    from graph_builder.builder import (
        NT_CLASS,
        NT_FILE,
        NT_FUNCTION,
        NT_MODULE,
        NT_REPO,
    )

    in_deg, out_deg = _compute_degrees(graph)
    graph_metrics = _compute_graph_metrics(graph)

    for node in graph.nodes:
        # Inject graph-level metrics (PageRank, HITS, Leiden) first so
        # per-type feature functions can see / override them if needed.
        node.features.update(graph_metrics[node.id])

        if node.type == NT_REPO:
            _compute_repo_features(node, graph, builder)
        elif node.type == NT_FILE:
            _compute_file_features(node, builder)
        elif node.type == NT_MODULE:
            _compute_module_features(node, builder, in_deg, out_deg)
        elif node.type == NT_CLASS:
            _compute_class_features(node, builder, in_deg, out_deg)
        elif node.type == NT_FUNCTION:
            _compute_function_features(node, builder, in_deg, out_deg)
