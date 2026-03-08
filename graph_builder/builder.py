"""
graph_builder.builder – Two-pass graph construction from a Python repository.

Limitations (v1)
-----------------
* Import resolution is best-effort based on the repo filesystem and the
  presence of ``__init__.py`` files (classic packages only).  Namespace
  packages are **not** supported.
* Call resolution is conservative: anything that cannot be confidently
  mapped to an internal definition becomes a SYMBOL node (``CALLS_SYMBOL``).
* No external type inference — only structural AST information is used.
* Nested function definitions (functions inside functions) are **skipped**
  in v1 to keep IDs simple and deterministic.
* Star imports (``from x import *``) create the ``IMPORTS_MODULE`` edge but
  do **not** expand the binding environment.
* Decorators and property descriptors are ignored.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from graph_builder.model import Edge, Graph, Node

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories whose names start with these are always skipped during the
# repository walk (e.g. .git, .tox, __pycache__).
_SKIP_DIR_PREFIXES = (".", "__pycache__")

# Node-type literals
NT_REPO = "repo"
NT_FILE = "file"
NT_MODULE = "module"
NT_CLASS = "class"
NT_FUNCTION = "function"
NT_SYMBOL = "symbol"

# Edge-type literals
ET_CONTAINS_FILE = "CONTAINS_FILE"
ET_IMPLEMENTS_MODULE = "IMPLEMENTS_MODULE"
ET_DEFINES_CLASS = "DEFINES_CLASS"
ET_DEFINES_FUNCTION = "DEFINES_FUNCTION"
ET_DEFINES_METHOD = "DEFINES_METHOD"
ET_IMPORTS_MODULE = "IMPORTS_MODULE"
ET_INHERITS = "INHERITS"
ET_CALLS = "CALLS"
ET_CALLS_SYMBOL = "CALLS_SYMBOL"

# Symbol scopes
SCOPE_LOCAL = "local"
SCOPE_MODULE = "module"
SCOPE_GLOBAL = "global"


# ---------------------------------------------------------------------------
# Helper: extract dotted name from an AST node
# ---------------------------------------------------------------------------

def _dotted_name(node: ast.expr) -> Optional[str]:
    """Try to extract a dotted name string from an AST expression node.

    Handles ``ast.Name`` and chains of ``ast.Attribute``.  Returns *None*
    for anything more complex (subscripts, calls, etc.).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if prefix is not None:
            return f"{prefix}.{node.attr}"
    return None


# ---------------------------------------------------------------------------
# GraphBuilder
# ---------------------------------------------------------------------------

class GraphBuilder:
    """Build a heterogeneous directed graph from a Python repository.

    Parameters
    ----------
    repo_root : Path
        Absolute (or relative) path to the repository root directory.
    repo_name : str
        Short human-readable name used in node IDs (e.g. ``"my_project"``).
    """

    def __init__(self, repo_root: Path, repo_name: str) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.repo_name = repo_name

        # -- graph state --
        self.graph = Graph()
        self._node_ids: Set[str] = set()
        self._edge_set: Set[Tuple[str, str, str]] = set()

        # -- lookup maps populated in passes --
        # Pass 0
        self._module_by_name: Dict[str, str] = {}          # module_name -> node_id
        self._module_by_file: Dict[str, str] = {}           # file_id   -> module_id
        self._file_by_module: Dict[str, str] = {}           # module_id -> file_id
        self._file_paths: List[Tuple[str, Path]] = []       # (relpath_str, abs_path) sorted

        # Pass 1
        self._ast_cache: Dict[str, ast.Module] = {}         # relpath_str -> parsed AST
        self._source_cache: Dict[str, str] = {}             # relpath_str -> source text
        self._symbol_index: Dict[Tuple[str, str], str] = {} # (module_name, qualname) -> node_id
        self._class_ids: Set[str] = set()
        self._func_ids: Set[str] = set()

        # Per-module binding environments (populated during Pass 2 import extraction)
        # module_name -> {alias_name: ("module", module_name) | ("symbol", "pkg.mod.Name")}
        self._bindings: Dict[str, Dict[str, Tuple[str, str]]] = defaultdict(dict)

        # Module-level definitions per module (for call resolution)
        # module_name -> {name: node_id} where name is unqualified
        self._local_defs: Dict[str, Dict[str, str]] = defaultdict(dict)

        # Classes defined per module (for constructor & self resolution)
        # module_name -> {class_name: class_node_id}
        self._local_classes: Dict[str, Dict[str, str]] = defaultdict(dict)

        # Populated after all INHERITS edges are built (between inheritance + calls sub-passes).
        # class_nid -> ordered list of parent class_nids (direct parents only; BFS used at query time)
        self._class_parents: Dict[str, List[str]] = defaultdict(list)

        # Jedi integration — enabled automatically if jedi is installed.
        self._use_jedi: bool = False
        self._jedi_project: Any = None
        try:
            self.enable_jedi()
        except ImportError:
            pass  # jedi not installed; fall back to pure-AST resolution

        # Package set — directories that contain __init__.py
        self._packages: Set[str] = set()

        # Parse-error counter
        self._parse_errors: int = 0

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def build(self) -> Graph:
        """Execute all passes and return the finished :class:`Graph`."""
        self.graph.metadata = {
            "repo_name": self.repo_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repo_root": str(self.repo_root),
        }

        self._pass0_scan_files()
        self._pass1_index_definitions()
        self._pass2_extract_relationships()

        if self._parse_errors:
            print(
                f"[graph_builder] WARNING: {self._parse_errors} file(s) could "
                "not be parsed and were skipped.",
                file=sys.stderr,
            )

        return self.graph

    # ------------------------------------------------------------------ #
    # Node / Edge helpers (dedup)                                          #
    # ------------------------------------------------------------------ #

    def _add_node(self, nid: str, ntype: str) -> Node:
        if nid not in self._node_ids:
            node = Node(id=nid, type=ntype)
            self.graph.add_node(node)
            self._node_ids.add(nid)
            return node
        # Return a dummy Node for chaining — the real one is already stored.
        return Node(id=nid, type=ntype)

    def _add_edge(self, source: str, etype: str, target: str) -> None:
        key = (source, etype, target)
        if key not in self._edge_set:
            self.graph.add_edge(Edge(source=source, type=etype, target=target))
            self._edge_set.add(key)

    # ------------------------------------------------------------------ #
    # ID helpers                                                           #
    # ------------------------------------------------------------------ #

    def _repo_id(self) -> str:
        return f"repo::{self.repo_name}"

    def _file_id(self, relpath: str) -> str:
        return f"file::{self.repo_name}::{relpath}"

    def _module_id(self, module_name: str) -> str:
        return f"mod::{self.repo_name}::{module_name}"

    def _class_id(self, module_name: str, qualname: str) -> str:
        return f"class::{self.repo_name}::{module_name}::{qualname}"

    def _func_id(self, module_name: str, qualname: str) -> str:
        return f"func::{self.repo_name}::{module_name}::{qualname}"

    def _symbol_id(self, scope: str, text: str) -> str:
        return f"sym::{self.repo_name}::{scope}::{text}"

    # ------------------------------------------------------------------ #
    # Module-name derivation                                               #
    # ------------------------------------------------------------------ #

    def _module_name_from_path(self, relpath: str) -> str:
        """Derive a dotted module name from a repo-relative path.

        Rules:
        * ``pkg/sub/__init__.py`` → ``"pkg.sub"``
        * ``pkg/sub/a.py``       → ``"pkg.sub.a"``
        * ``__init__.py`` (root) → ``""`` (empty string — root package)
        """
        p = Path(relpath)
        if p.name == "__init__.py":
            parts = p.parent.parts
            return ".".join(parts) if parts else ""
        else:
            # Strip the .py suffix and replace path separators with dots.
            return ".".join(p.with_suffix("").parts)

    # ------------------------------------------------------------------ #
    # Pass 0 – Repository scan + module mapping                            #
    # ------------------------------------------------------------------ #

    def _pass0_scan_files(self) -> None:
        """Walk the repository for *.py files, create repo/file/module nodes
        and the initial containment edges."""

        repo_id = self._repo_id()
        self._add_node(repo_id, NT_REPO)

        # First pass: discover packages (dirs with __init__.py)
        self._packages = set()
        for init_path in sorted(self.repo_root.rglob("__init__.py")):
            if self._should_skip(init_path):
                continue
            # Record every ancestor package between repo_root and the init dir
            pkg_dir = init_path.parent
            rel = pkg_dir.relative_to(self.repo_root)
            # Add this dir and every parent dir up to (but not including) repo_root
            parts = rel.parts
            for i in range(len(parts)):
                self._packages.add("/".join(parts[: i + 1]))
            # Root package (when __init__.py is at repo root)
            if rel == Path("."):
                self._packages.add("")

        # Collect all .py files, sorted for determinism
        py_files: List[Tuple[str, Path]] = []
        for abs_path in sorted(self.repo_root.rglob("*.py")):
            if self._should_skip(abs_path):
                continue
            relpath_str = str(abs_path.relative_to(self.repo_root))
            py_files.append((relpath_str, abs_path))

        for relpath_str, abs_path in py_files:
            file_nid = self._file_id(relpath_str)
            module_name = self._module_name_from_path(relpath_str)
            module_nid = self._module_id(module_name)

            # Nodes
            self._add_node(file_nid, NT_FILE)
            self._add_node(module_nid, NT_MODULE)

            # Edges
            self._add_edge(repo_id, ET_CONTAINS_FILE, file_nid)
            self._add_edge(file_nid, ET_IMPLEMENTS_MODULE, module_nid)

            # Maps
            self._module_by_name[module_name] = module_nid
            self._module_by_file[file_nid] = module_nid
            self._file_by_module[module_nid] = file_nid

            self._file_paths.append((relpath_str, abs_path))

    def _should_skip(self, path: Path) -> bool:
        """Return True if *path* should be excluded from the scan."""
        for part in path.relative_to(self.repo_root).parts:
            if any(part.startswith(pfx) for pfx in _SKIP_DIR_PREFIXES):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Pass 1 – Definition index                                            #
    # ------------------------------------------------------------------ #

    def _pass1_index_definitions(self) -> None:
        """Parse every .py file and create class / function nodes with their
        definition edges."""

        for relpath_str, abs_path in self._file_paths:
            module_name = self._module_name_from_path(relpath_str)
            tree = self._parse_file(relpath_str, abs_path)
            if tree is None:
                continue

            module_nid = self._module_id(module_name)
            self._index_body(
                tree.body,
                module_name=module_name,
                module_nid=module_nid,
                parent_nid=module_nid,
                parent_kind="module",
                class_chain=[],
            )

    def _index_body(
        self,
        body: List[ast.stmt],
        *,
        module_name: str,
        module_nid: str,
        parent_nid: str,
        parent_kind: str,       # "module" | "class"
        class_chain: List[str],
    ) -> None:
        """Recursively index definitions in *body*.

        Parameters
        ----------
        class_chain
            List of enclosing class names (for nested classes), e.g.
            ``["Outer", "Inner"]`` means we are inside ``Outer.Inner``.
        """
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                qual = ".".join(class_chain + [stmt.name])
                cls_nid = self._class_id(module_name, qual)
                self._add_node(cls_nid, NT_CLASS)
                self._class_ids.add(cls_nid)

                if parent_kind == "module":
                    self._add_edge(parent_nid, ET_DEFINES_CLASS, cls_nid)
                else:
                    # Nested class inside another class — still use DEFINES_CLASS
                    self._add_edge(parent_nid, ET_DEFINES_CLASS, cls_nid)

                # Track in local classes map (only top-level class name for binding)
                if not class_chain:
                    self._local_classes[module_name][stmt.name] = cls_nid

                # Symbol index entry
                self._symbol_index[(module_name, qual)] = cls_nid

                # Recurse into class body
                self._index_body(
                    stmt.body,
                    module_name=module_name,
                    module_nid=module_nid,
                    parent_nid=cls_nid,
                    parent_kind="class",
                    class_chain=class_chain + [stmt.name],
                )

            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(class_chain + [stmt.name]) if class_chain else stmt.name
                func_nid = self._func_id(module_name, qual)
                self._add_node(func_nid, NT_FUNCTION)
                self._func_ids.add(func_nid)

                if parent_kind == "class":
                    self._add_edge(parent_nid, ET_DEFINES_METHOD, func_nid)
                else:
                    self._add_edge(parent_nid, ET_DEFINES_FUNCTION, func_nid)

                # Track in local defs (module-level functions and class methods)
                if not class_chain:
                    self._local_defs[module_name][stmt.name] = func_nid

                # Symbol index entry
                self._symbol_index[(module_name, qual)] = func_nid

                # NOTE: nested functions (defs inside this function) are
                # intentionally skipped in v1 to keep IDs simple.

    # ------------------------------------------------------------------ #
    # AST Parsing helper (with caching)                                    #
    # ------------------------------------------------------------------ #

    def _parse_file(self, relpath: str, abs_path: Path) -> Optional[ast.Module]:
        """Parse a Python file into an AST, caching the result.

        Returns *None* if the file cannot be read or parsed (error is logged
        to stderr and the counter is bumped).
        """
        if relpath in self._ast_cache:
            return self._ast_cache[relpath]
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=relpath)
        except SyntaxError as exc:
            print(f"[graph_builder] SyntaxError in {relpath}: {exc}", file=sys.stderr)
            self._parse_errors += 1
            return None
        except Exception as exc:  # pragma: no cover — unexpected errors
            print(f"[graph_builder] Error reading {relpath}: {exc}", file=sys.stderr)
            self._parse_errors += 1
            return None
        self._ast_cache[relpath] = tree
        self._source_cache[relpath] = source
        return tree

    # ------------------------------------------------------------------ #
    # Pass 2 – Relationship extraction                                     #
    # ------------------------------------------------------------------ #

    def _pass2_extract_relationships(self) -> None:
        """Extract imports, inheritance, and call edges.

        Sub-pass order:
        1. Imports  — builds ``_bindings`` used by all subsequent passes.
        2. Inheritance — builds INHERITS edges for *all* files before calls;
           this is required so ``_build_class_parents()`` can see the full
           inheritance graph before call resolution begins.
        3. ``_build_class_parents()`` — indexes direct parent lists for MRO
           lookup during self.method and cls.method resolution.
        4. Calls — resolves call sites using the complete definition index,
           binding environment, and parent-class tables.
        """
        # Sub-pass 1: imports
        for relpath_str, _abs_path in self._file_paths:
            if relpath_str not in self._ast_cache:
                continue
            tree = self._ast_cache[relpath_str]
            module_name = self._module_name_from_path(relpath_str)
            self._extract_imports(tree, module_name)

        # Sub-pass 2: inheritance (all files before calls)
        for relpath_str, _abs_path in self._file_paths:
            if relpath_str not in self._ast_cache:
                continue
            tree = self._ast_cache[relpath_str]
            module_name = self._module_name_from_path(relpath_str)
            self._extract_inheritance(tree, module_name)

        # Sub-pass 3: build parent-class index
        self._build_class_parents()

        # Sub-pass 4: call extraction
        for relpath_str, abs_path in self._file_paths:
            if relpath_str not in self._ast_cache:
                continue
            tree = self._ast_cache[relpath_str]
            module_name = self._module_name_from_path(relpath_str)
            self._extract_calls(tree, module_name, relpath_str, abs_path)

    # ------------------------------------------------------------------ #
    # 2-A  Imports                                                         #
    # ------------------------------------------------------------------ #

    def _extract_imports(self, tree: ast.Module, module_name: str) -> None:
        """Walk import statements and emit IMPORTS_MODULE edges.

        Also populates ``self._bindings[module_name]`` for later symbol
        resolution.

        Limitations
        -----------
        * Star imports create the module edge but do **not** expand the
          binding environment since we would need to parse the target module's
          ``__all__`` or public namespace.
        """
        mod_nid = self._module_id(module_name)
        bindings = self._bindings[module_name]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # import a.b.c as x  →  target module "a.b.c"
                    target_name = alias.name
                    local_alias = alias.asname or alias.name.split(".")[-1]

                    if target_name in self._module_by_name:
                        self._add_edge(mod_nid, ET_IMPORTS_MODULE,
                                       self._module_by_name[target_name])
                        bindings[local_alias] = ("module", target_name)
                    else:
                        # Could be a partially matching package — try to find
                        # the most specific internal module.
                        resolved = self._resolve_partial_module(target_name)
                        if resolved:
                            self._add_edge(mod_nid, ET_IMPORTS_MODULE,
                                           self._module_by_name[resolved])
                            bindings[local_alias] = ("module", resolved)

            elif isinstance(node, ast.ImportFrom):
                base_module = self._resolve_import_from(node, module_name)
                if base_module is None:
                    continue

                # Edge from this module to the base module (if internal)
                if base_module in self._module_by_name:
                    self._add_edge(mod_nid, ET_IMPORTS_MODULE,
                                   self._module_by_name[base_module])

                for alias in node.names:
                    if alias.name == "*":
                        # Star import — edge already added, skip bindings
                        continue
                    local_alias = alias.asname or alias.name

                    # Check if the imported name is itself a sub-module
                    sub_module = f"{base_module}.{alias.name}" if base_module else alias.name
                    if sub_module in self._module_by_name:
                        self._add_edge(mod_nid, ET_IMPORTS_MODULE,
                                       self._module_by_name[sub_module])
                        bindings[local_alias] = ("module", sub_module)
                    else:
                        # It's a symbol (class, function, variable) inside base_module
                        bindings[local_alias] = ("symbol", f"{base_module}.{alias.name}")

    def _resolve_import_from(
        self, node: ast.ImportFrom, current_module: str
    ) -> Optional[str]:
        """Resolve the base module of an ``ast.ImportFrom`` node.

        Handles relative imports by computing the package prefix from
        *current_module* and ``node.level``.
        """
        if node.level == 0:
            # Absolute import
            return node.module  # may be None for bare ``from . import x``

        # Relative import — compute anchor package
        parts = current_module.split(".") if current_module else []
        # ``level`` of 1 means same package, 2 means parent, etc.
        # We strip (level - 1) components from the end — the first dot means
        # "current package" which is the module's parent.
        # But if the module IS an __init__ (its file is __init__.py), the
        # package is the module itself, not its parent.
        module_nid = self._module_by_name.get(current_module)
        file_nid = self._file_by_module.get(module_nid, "") if module_nid else ""
        is_pkg_init = file_nid.endswith("__init__.py")

        if not is_pkg_init:
            # Module ``a.b.c`` lives in package ``a.b``
            parts = parts[:-1] if parts else []

        # Now go up (level - 1) more levels
        levels_up = node.level - 1
        if levels_up > len(parts):
            return None  # relative import goes above repo root — skip
        parts = parts[: len(parts) - levels_up] if levels_up else parts

        base = ".".join(parts)
        if node.module:
            return f"{base}.{node.module}" if base else node.module
        return base if base else None

    def _resolve_partial_module(self, target: str) -> Optional[str]:
        """Try to match a dotted import to the most specific internal module.

        E.g. ``import a.b.c`` might only have ``a.b`` as a module in the repo.
        We return the longest prefix that exists.
        """
        parts = target.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in self._module_by_name:
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # 2-B  Inheritance                                                     #
    # ------------------------------------------------------------------ #

    def _extract_inheritance(self, tree: ast.Module, module_name: str) -> None:
        """Walk ClassDef nodes and emit INHERITS edges for resolved bases."""
        self._walk_classes_for_inheritance(tree.body, module_name, [])

    def _walk_classes_for_inheritance(
        self,
        body: List[ast.stmt],
        module_name: str,
        class_chain: List[str],
    ) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                qual = ".".join(class_chain + [stmt.name])
                cls_nid = self._class_id(module_name, qual)

                for base in stmt.bases:
                    base_name = _dotted_name(base)
                    if base_name is None:
                        continue
                    resolved = self._resolve_class_ref(base_name, module_name)
                    if resolved and resolved in self._class_ids:
                        self._add_edge(cls_nid, ET_INHERITS, resolved)

                # Recurse into nested classes
                self._walk_classes_for_inheritance(
                    stmt.body, module_name, class_chain + [stmt.name]
                )

    def _resolve_class_ref(
        self, name: str, module_name: str
    ) -> Optional[str]:
        """Best-effort resolution of a class reference to an internal class
        node id.

        Resolution order:
        1. Same-module class (e.g. ``Base``).
        2. Imported binding (e.g. ``from pkg.models import Base``).
        """
        # 1) Same-module lookup
        candidate = (module_name, name)
        if candidate in self._symbol_index:
            nid = self._symbol_index[candidate]
            if nid in self._class_ids:
                return nid

        # 2) Imported binding
        bindings = self._bindings.get(module_name, {})
        parts = name.split(".")
        root = parts[0]
        if root in bindings:
            kind, value = bindings[root]
            if kind == "symbol":
                # value is e.g. "pkg.mod.ClassName" — the last component is the
                # class name, the rest is the module.
                vparts = value.rsplit(".", 1)
                if len(vparts) == 2:
                    target_mod, target_cls = vparts
                    # If there are remaining parts after root, append them
                    remaining = parts[1:]
                    target_qual = ".".join([target_cls] + remaining)
                    cand = (target_mod, target_qual)
                    if cand in self._symbol_index:
                        nid = self._symbol_index[cand]
                        if nid in self._class_ids:
                            return nid
            elif kind == "module":
                # name like "mod.ClassName" where root is module alias
                target_mod = value
                remaining = parts[1:]
                if remaining:
                    target_qual = ".".join(remaining)
                    cand = (target_mod, target_qual)
                    if cand in self._symbol_index:
                        nid = self._symbol_index[cand]
                        if nid in self._class_ids:
                            return nid

        return None

    # ------------------------------------------------------------------ #
    # 2-B.5  Build class parent index (runs after all INHERITS edges)     #
    # ------------------------------------------------------------------ #

    def _build_class_parents(self) -> None:
        """Index direct parent classes from INHERITS edges.

        Must be called *after* all ``_extract_inheritance`` runs so the full
        graph is available.  Populates ``self._class_parents``.
        """
        for edge in self.graph.edges:
            if edge.type == ET_INHERITS:
                self._class_parents[edge.source].append(edge.target)

    # ------------------------------------------------------------------ #
    # 2-C  Calls                                                           #
    # ------------------------------------------------------------------ #

    def _extract_calls(
        self,
        tree: ast.Module,
        module_name: str,
        relpath_str: str,
        abs_path: "Path",
    ) -> None:
        """Walk function bodies and emit CALLS / CALLS_SYMBOL edges."""
        # Build a per-file jedi.Script when jedi integration is enabled.
        jedi_script: Any = None
        if self._use_jedi and self._jedi_project is not None:
            try:
                import jedi  # type: ignore
                source = self._source_cache.get(relpath_str, "")
                jedi_script = jedi.Script(
                    code=source,
                    path=str(abs_path),
                    project=self._jedi_project,
                )
            except Exception:
                pass
        self._walk_for_calls(
            tree.body, module_name, enclosing_func=None,
            class_chain=[], jedi_script=jedi_script,
        )

    def _walk_for_calls(
        self,
        body: List[ast.stmt],
        module_name: str,
        enclosing_func: Optional[str],  # func node-id
        class_chain: List[str],
        jedi_script: Any = None,
    ) -> None:
        """Recursively descend into classes and functions, collecting calls."""
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                self._walk_for_calls(
                    stmt.body,
                    module_name,
                    enclosing_func=None,
                    class_chain=class_chain + [stmt.name],
                    jedi_script=jedi_script,
                )
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(class_chain + [stmt.name]) if class_chain else stmt.name
                func_nid = self._func_id(module_name, qual)
                # Walk the entire function body for Call nodes
                self._collect_calls_in(
                    stmt, module_name, func_nid, class_chain,
                    jedi_script=jedi_script,
                )

    def _collect_calls_in(
        self,
        func_node: ast.AST,
        module_name: str,
        func_nid: str,
        class_chain: List[str],
        jedi_script: Any = None,
    ) -> None:
        """Find every ``ast.Call`` inside *func_node* and resolve it."""
        enclosing_class = class_chain[-1] if class_chain else None

        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue

            callee = node.func
            callee_str = _dotted_name(callee)

            if callee_str is None:
                # Complex expression — emit generic unresolved symbol
                self._emit_calls_symbol(func_nid, SCOPE_LOCAL, "<dynamic>")
                continue

            parts = callee_str.split(".")
            resolved = self._try_resolve_call(
                parts, module_name, enclosing_class
            )
            if resolved is None and jedi_script is not None:
                resolved = self._resolve_with_jedi(node, jedi_script)
            if resolved:
                self._add_edge(func_nid, ET_CALLS, resolved)
            else:
                # Emit CALLS_SYMBOL
                scope, text = self._classify_unresolved(
                    parts, module_name, enclosing_class
                )
                self._emit_calls_symbol(func_nid, scope, text)

    # -- call resolution ---------------------------------------------------

    def _try_resolve_call(
        self,
        parts: List[str],
        module_name: str,
        enclosing_class: Optional[str],
    ) -> Optional[str]:
        """Attempt to map a callee (split into dotted parts) to an internal
        function node id.

        Returns the node id on success, or *None* if resolution fails.
        """
        bindings = self._bindings.get(module_name, {})

        if len(parts) == 1:
            name = parts[0]
            return self._resolve_simple_call(name, module_name, bindings)

        if len(parts) == 2:
            obj, method = parts
            return self._resolve_two_part_call(
                obj, method, module_name, enclosing_class, bindings
            )

        # 3+ parts — try module alias resolution for the root
        root = parts[0]
        if root in bindings:
            kind, value = bindings[root]
            if kind == "module":
                target_qual = ".".join(parts[1:])
                # Could be a function in that module
                cand = (value, target_qual)
                if cand in self._symbol_index and self._symbol_index[cand] in self._func_ids:
                    return self._symbol_index[cand]
                # Could be a class method: e.g. mod.Class.method
                # We just try the qualname as-is
                return None

        return None

    def _resolve_simple_call(
        self,
        name: str,
        module_name: str,
        bindings: Dict[str, Tuple[str, str]],
    ) -> Optional[str]:
        """Resolve a single-name call like ``foo()``."""
        # 1) Local module-level function def
        local = self._local_defs.get(module_name, {})
        if name in local:
            nid = local[name]
            if nid in self._func_ids:
                return nid

        # 2) Constructor call — name matches a local class
        local_cls = self._local_classes.get(module_name, {})
        if name in local_cls:
            # Try to find __init__ method
            init_qual = f"{name}.__init__"
            cand = (module_name, init_qual)
            if cand in self._symbol_index and self._symbol_index[cand] in self._func_ids:
                return self._symbol_index[cand]
            # Class exists but no __init__ (inherits from builtins/external).
            # Return the class node itself — still valuable as a resolved CALLS edge.
            return local_cls[name]

        # 3) Imported symbol
        if name in bindings:
            kind, value = bindings[name]
            if kind == "symbol":
                # value is "pkg.mod.funcname"
                vparts = value.rsplit(".", 1)
                if len(vparts) == 2:
                    target_mod, target_sym = vparts
                    cand = (target_mod, target_sym)
                    if cand in self._symbol_index:
                        nid = self._symbol_index[cand]
                        if nid in self._func_ids:
                            return nid
                        # Could be a class — try constructor
                        if nid in self._class_ids:
                            init_cand = (target_mod, f"{target_sym}.__init__")
                            if init_cand in self._symbol_index and \
                               self._symbol_index[init_cand] in self._func_ids:
                                return self._symbol_index[init_cand]
                            # No __init__ — return the class node directly.
                            return nid
            elif kind == "module":
                # Calling a module? Unusual, but skip.
                pass

        return None

    def _resolve_two_part_call(
        self,
        obj: str,
        method: str,
        module_name: str,
        enclosing_class: Optional[str],
        bindings: Dict[str, Tuple[str, str]],
    ) -> Optional[str]:
        """Resolve ``obj.method()``."""
        # 1) self.method inside a class (with MRO walk-up)
        if obj == "self" and enclosing_class:
            qual = f"{enclosing_class}.{method}"
            cand = (module_name, qual)
            if cand in self._symbol_index and self._symbol_index[cand] in self._func_ids:
                return self._symbol_index[cand]
            # Not found directly — try parent classes via BFS
            cls_nid = self._class_id(module_name, enclosing_class)
            hit = self._lookup_method_in_parents(cls_nid, method)
            if hit:
                return hit

        # 2) cls.method (classmethod convention, with MRO walk-up)
        if obj == "cls" and enclosing_class:
            qual = f"{enclosing_class}.{method}"
            cand = (module_name, qual)
            if cand in self._symbol_index and self._symbol_index[cand] in self._func_ids:
                return self._symbol_index[cand]
            cls_nid = self._class_id(module_name, enclosing_class)
            hit = self._lookup_method_in_parents(cls_nid, method)
            if hit:
                return hit

        # 3) super().method — resolve to the same class method (best-effort)
        #    Actually super() is a Call, so this would be "<dynamic>.method".
        #    We'll handle it in unresolved path.

        # 4) Module-alias.function  (e.g. os.path or models.MyFunc)
        if obj in bindings:
            kind, value = bindings[obj]
            if kind == "module":
                cand = (value, method)
                if cand in self._symbol_index:
                    nid = self._symbol_index[cand]
                    if nid in self._func_ids:
                        return nid
                    # Could be a class — try constructor; fall back to class node.
                    if nid in self._class_ids:
                        init_cand = (value, f"{method}.__init__")
                        if init_cand in self._symbol_index and \
                           self._symbol_index[init_cand] in self._func_ids:
                            return self._symbol_index[init_cand]
                        return nid
            elif kind == "symbol":
                # obj is an imported symbol (maybe a class instance?)
                # e.g. from pkg import obj -> obj.method()
                # Hard to resolve without type info — skip.
                pass

        # 5) ClassName.method (direct class reference in same module)
        local_cls = self._local_classes.get(module_name, {})
        if obj in local_cls:
            qual = f"{obj}.{method}"
            cand = (module_name, qual)
            if cand in self._symbol_index and self._symbol_index[cand] in self._func_ids:
                return self._symbol_index[cand]
            # Also try parent classes
            hit = self._lookup_method_in_parents(local_cls[obj], method)
            if hit:
                return hit

        return None

    def _lookup_method_in_parents(self, cls_nid: str, method: str) -> Optional[str]:
        """BFS over the parent-class graph to find *method* in an ancestor.

        Returns the function node id of the first matching method, or *None*.
        Avoids cycles with a visited set.
        """
        visited: Set[str] = set()
        queue: List[str] = list(self._class_parents.get(cls_nid, []))
        while queue:
            parent_nid = queue.pop(0)
            if parent_nid in visited:
                continue
            visited.add(parent_nid)
            # Decode parent module + qualname from the node id
            # Format: "class::<repo>::<module>::<qual>"
            parts = parent_nid.split("::", 3)
            if len(parts) != 4:
                continue
            _kind, _repo, parent_mod, parent_qual = parts
            cand = (parent_mod, f"{parent_qual}.{method}")
            if cand in self._symbol_index:
                nid = self._symbol_index[cand]
                if nid in self._func_ids:
                    return nid
            # Enqueue grandparents
            queue.extend(
                p for p in self._class_parents.get(parent_nid, [])
                if p not in visited
            )
        return None

    # -- jedi-backed resolution -------------------------------------------

    def _resolve_with_jedi(self, call_node: ast.Call, jedi_script: Any) -> Optional[str]:
        """Use jedi's ``goto()`` to resolve a call to an internal definition.

        *jedi_script* is a ``jedi.Script`` pre-built for the current file.
        Returns an internal function/class node id, or *None*.
        """
        callee = call_node.func
        try:
            # goto() at the end of the callee expression finds the definition.
            line = getattr(callee, "end_lineno", getattr(callee, "lineno", None))
            col = getattr(callee, "end_col_offset", getattr(callee, "col_offset", None))
            if line is None or col is None:
                return None
            names = jedi_script.goto(line=line, column=col)
        except Exception:
            return None

        for name in names:
            mod_path = getattr(name, "module_path", None)
            if mod_path is None:
                continue
            import os as _os
            mod_path_str = str(mod_path)
            repo_root_str = str(self.repo_root)
            if not mod_path_str.startswith(repo_root_str):
                continue  # External library — no node in our graph
            # Map file path to module name
            rel = mod_path_str[len(repo_root_str):].lstrip(_os.sep).replace(_os.sep, "/")
            target_module = self._module_name_from_path(rel)
            if not target_module:
                continue
            # jedi's full_name may be like "django.apps.config.AppConfig.method"
            full_name: Optional[str] = getattr(name, "full_name", None)
            sym_name: str = name.name
            candidates: List[Tuple[str, str]] = []
            if full_name and full_name.startswith(target_module + "."):
                qual = full_name[len(target_module) + 1:]
                candidates.append((target_module, qual))
            candidates.append((target_module, sym_name))
            for cand in candidates:
                if cand in self._symbol_index:
                    nid = self._symbol_index[cand]
                    if nid in self._func_ids or nid in self._class_ids:
                        return nid
        return None

    # -----------------------------------------------------------------------
    # Public helpers
    # -----------------------------------------------------------------------

    def enable_jedi(self) -> None:
        """Enable jedi-backed call resolution (Pass 2 calls sub-pass).

        Requires ``jedi`` to be installed (``pip install jedi``).  When
        enabled, every call site that could not be resolved via the pure-AST
        heuristics is passed to ``jedi.Script.goto()`` for type-inference–
        backed resolution.  This significantly improves resolution of
        ``var.method()``, ``self.attr.method()``, and cross-module calls but
        adds substantial runtime overhead on large repos (minutes on Django).
        """
        try:
            import jedi  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "jedi is required for --use-jedi.  Install it with: pip install jedi"
            ) from exc
        import jedi as _jedi  # type: ignore
        self._use_jedi = True
        self._jedi_project = _jedi.Project(path=str(self.repo_root))

    def _classify_unresolved(
        self,
        parts: List[str],
        module_name: str,
        enclosing_class: Optional[str],
    ) -> Tuple[str, str]:
        """Determine (scope, text) for an unresolved call.

        Returns
        -------
        scope : str
            One of ``"local"``, ``"module"``, ``"global"``.
        text : str
            Normalised symbol text.
        """
        bindings = self._bindings.get(module_name, {})

        if len(parts) == 1:
            return (SCOPE_LOCAL, parts[0])

        if len(parts) == 2 and parts[0] in ("self", "cls") and enclosing_class:
            # self.method / cls.method → module-scoped with ClassName.method
            return (SCOPE_MODULE, f"{enclosing_class}.{parts[1]}")

        # If the root is a module alias, expand it
        root = parts[0]
        if root in bindings:
            kind, value = bindings[root]
            if kind == "module":
                text = value + "." + ".".join(parts[1:])
                return (SCOPE_GLOBAL, text)
            elif kind == "symbol":
                text = value + "." + ".".join(parts[1:]) if len(parts) > 1 else value
                return (SCOPE_GLOBAL, text)

        # Dotted name without binding — treat as global guess
        if len(parts) >= 2:
            return (SCOPE_GLOBAL, ".".join(parts))

        return (SCOPE_LOCAL, ".".join(parts))

    def _emit_calls_symbol(self, func_nid: str, scope: str, text: str) -> None:
        """Create (if needed) a symbol node and add a CALLS_SYMBOL edge."""
        sym_nid = self._symbol_id(scope, text)
        self._add_node(sym_nid, NT_SYMBOL)
        self._add_edge(func_nid, ET_CALLS_SYMBOL, sym_nid)
