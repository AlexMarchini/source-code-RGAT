# Source Code RGAT Capstone Project

## graph_builder – Python Code Graph Builder

Analyses any Python repository using the built-in `ast` module and constructs a
**heterogeneous directed graph** suitable for R-GAT / GNN workloads.  
**Standard-library only** — no external dependencies.

### Quick start

```bash
# Run from the repository root
python -m graph_builder \
    --repo_root /path/to/target/repo \
    --repo_name my_project \
    --out graph.json
```

### Programmatic usage

```python
from pathlib import Path
from graph_builder import GraphBuilder

graph = GraphBuilder(
    repo_root=Path("/path/to/target/repo"),
    repo_name="my_project",
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
  "metadata": { "repo_name": "...", "created_at": "ISO-8601", "repo_root": "..." },
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

