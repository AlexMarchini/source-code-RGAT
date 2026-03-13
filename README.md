# Source Code RGAT Capstone Project

## graph_builder – Python Code Graph Builder

Analyses any Python repository using the built-in `ast` module and constructs a
**heterogeneous directed graph** suitable for R-GAT / GNN workloads.

### Installation

```bash
pip install -r requirements.txt
```

This installs [igraph](https://igraph.org/) and
[leidenalg](https://leidenalg.readthedocs.io/), used to compute graph-level
node features (PageRank, HITS hub/authority scores, Leiden communities).

### Quick start

```bash
# Single repository (legacy syntax)
python -m graph_builder \
    --repo_root /path/to/target/repo \
    --repo_name my_project \
    --out graph.json

# Multi-repository (preferred for cross-project analysis)
python -m graph_builder \
    --repo django:/path/to/django \
    --repo drf:/path/to/djangorestframework \
    --repo wagtail:/path/to/wagtail \
    --out graph.json
```

### Programmatic usage

```python
from pathlib import Path
from graph_builder import GraphBuilder

# Single repo (backward compatible)
graph = GraphBuilder(
    repo_root=Path("/path/to/target/repo"),
    repo_name="my_project",
).build()

# Multi-repo — cross-repo imports, inheritance, and calls are resolved
graph = GraphBuilder(
    repos=[
        (Path("/path/to/django"), "django"),
        (Path("/path/to/drf"), "drf"),
        (Path("/path/to/wagtail"), "wagtail"),
    ],
).build()

graph.write_json("graph.json")
print(graph.summary())
```

### Node types

| Type       | ID pattern                                          | Description                          |
|------------|-----------------------------------------------------|--------------------------------------|
| `repo`     | `repo::<name>`                                      | Repository root                      |
| `file`     | `file::<name>::<relpath>`                            | Python source file                   |
| `module`   | `mod::<name>::<dotted.module>`                       | Python module                        |
| `class`    | `class::<name>::<module>::<Qual>`                    | Class definition                     |
| `function` | `func::<name>::<module>::<qual>`                     | Function or method definition        |
| `symbol`   | `sym::<name>::<scope>::<text>`                       | Unresolved / external call target    |

### Edge types

| Edge                 | Source     | Target     |
|----------------------|------------|------------|
| `CONTAINS_FILE`      | repo       | file       |
| `IMPLEMENTS_MODULE`  | file       | module     |
| `DEFINES_CLASS`      | module     | class      |
| `DEFINES_FUNCTION`   | module     | function   |
| `DEFINES_METHOD`     | class      | function   |
| `IMPORTS_MODULE`     | module     | module     |
| `INHERITS`           | class      | class      |
| `CALLS`              | function   | function   |
| `CALLS_SYMBOL`       | function   | symbol     |

### Output format

```json
{
  "metadata": {
    "repos": [{"name": "django", "root": "/..."}, {"name": "drf", "root": "/..."}],
    "repo_name": "django",
    "created_at": "ISO-8601",
    "repo_root": "..."
  },
  "nodes": [ { "id": "...", "type": "module" }, ... ],
  "edges": [ { "source": "...", "type": "IMPORTS_MODULE", "target": "..." }, ... ]
}
```

### Limitations (v1)

- Import resolution is best-effort (filesystem + `__init__.py` classic packages only).
- Call resolution is conservative — unresolved calls become `SYMBOL` nodes.
- No external type inference.
- Nested function definitions (functions inside functions) are skipped.
- Star imports create the module edge but don't expand bindings.
- Namespace packages are not supported.

