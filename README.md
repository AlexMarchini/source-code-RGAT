# Source Code RGAT — Multi-Scale Relational Graph Attention Network for Code Risk Assessment

A multi-scale Relational Graph Attention Network (RGAT) trained on heterogeneous
source-code graphs from 13 Django ecosystem repositories. The model learns to
predict structural code relationships (function calls, class inheritance, module
imports) and augments LLM-based pull-request risk assessments with graph-derived
structural context.

## Key results

| Metric | Value |
|--------|-------|
| Link prediction ROC-AUC | 0.827 |
| Link prediction AP | 0.842 |
| RGAT-augmented win rate (55 real PRs) | 69.1 % |
| Mean judge score (augmented vs baseline) | 19.44 vs 17.45 |

## Installation

```bash
pip install -r requirements.txt
```

**Dependencies:** PyTorch >= 2.0, PyG >= 2.5, sentence-transformers >= 2.2,
scikit-learn >= 1.0, pandas >= 1.5, igraph >= 0.11, leidenalg >= 0.10.

## Project structure

```
├── graph_builder/        # AST-based code graph extraction tool
├── rgat/                 # Core RGAT model, training, and evaluation modules
├── notebooks/
│   ├── 01_preprocessing.ipynb    # Graph loading, cleaning, feature encoding, edge splitting
│   ├── 02_training.ipynb         # RGAT training with diversity regularization
│   ├── 03_evaluation.ipynb       # Per-edge-type metrics, attention analysis
│   ├── 04_pr_collection_llm_baseline.ipynb  # Collect 55 real PRs, LLM-only risk assessment
│   ├── 05_rgat_inference_augmented_assessment.ipynb  # RGAT-augmented risk assessment
│   └── eda/                      # Exploratory data analysis
├── paper/                # Methodology, results, and conclusion sections
├── prompts/              # LLM assessment prompts and judge evaluation framework
├── artifacts/            # Preprocessed graph data and training artifacts
├── model_output/         # Trained model checkpoint and metadata
├── cache/                # Cached sentence-transformer embeddings
├── checkpoints/          # Training checkpoints
├── django_ecosystem_v1.json      # Django ecosystem graph (~241 MB)
├── run_pipeline.py       # End-to-end pipeline runner
└── requirements.txt
```

## Quick start

### End-to-end pipeline

```bash
python run_pipeline.py
```

Runs all stages (preprocessing, training, evaluation) with per-stage
wall-clock timing.

### Training via CLI

```bash
python -m rgat --json django_ecosystem_v1.json \
    --hidden-dim 128 --num-heads 8 --num-layers 3 \
    --lr 1e-3 --epochs 200 --patience 25 \
    --device cuda
```

All configurable arguments: `--json`, `--hidden-dim`, `--num-heads`,
`--num-layers`, `--dropout`, `--lr`, `--epochs`, `--val-ratio`, `--val-every`,
`--patience`, `--neg-ratio`, `--leiden-embed-dim`, `--sentence-model`,
`--cache-dir`, `--checkpoint-dir`, `--device`.

### Graph construction

```bash
# Single repository
python -m graph_builder \
    --repo_root /path/to/repo --repo_name my_project --out graph.json

# Multi-repository (preferred — resolves cross-repo imports/calls)
python -m graph_builder \
    --repo django:/path/to/django \
    --repo drf:/path/to/drf \
    --out graph.json
```

## Model architecture

**HeteroRGATEncoder** — stacked GATv2 layers with multi-scale attention:

- Per-type input projection: scalar features ∥ sentence embedding (384-d) ∥
  Leiden community embedding (16-d) → `hidden_dim`
- 2–3 `MultiScaleHeteroConv` layers, each with:
  - **Local branch** (`num_heads // 2`): standard 1-hop GATConv
  - **Global branch** (remaining heads): augmented edges via structural roles,
    2-hop random walks, and degree bucketing
  - Per-relation softmax gating; learned sigmoid gate combining local/global
- LayerNorm, residual connections, ELU activation, dropout
- L2-normalized output embeddings (~3M parameters)

**RelationDecoder** — DistMult factorization: `score = (z_src * r_rel * z_dst).sum(-1)`

**AttentionDiversityLoss** — regularizes head diversity via orthogonality (0.4),
entropy maximization (0.3), and Gini-coefficient sparsity (0.3). Activated after
a 10-epoch warmup.

### Training hyperparameters

| Parameter | Default |
|-----------|---------|
| `hidden_dim` | 128 |
| `num_heads` | 8 |
| `num_layers` | 3 |
| `dropout` | 0.2 |
| `lr` | 1e-3 |
| `weight_decay` | 1e-4 |
| `epochs` | 200 |
| `patience` | 25 |
| `val_ratio` | 0.15 |
| `neg_sampling_ratio` | 1.0 |
| `diversity_loss_weight` | 1.0 |
| `supervised_relations` | CALLS, INHERITS, IMPORTS_MODULE |

## Pipeline overview

### 1. Graph construction (`graph_builder/`)

Two-pass AST analysis over Python repositories:

1. **Index pass** — collect all definitions (modules, classes, functions)
2. **Relationship pass** — extract imports, inheritance, calls with
   scope-aware name resolution and MRO traversal

Computes per-node features: McCabe cyclomatic complexity, nesting depth,
line counts, PageRank, HITS hub/authority scores, and Leiden communities.

### 2. Preprocessing (notebook 01)

- Schema validation (strict per-node-type feature checks)
- Data cleaning: collapse file nodes, remove empty `__init__` hub edges,
  prune self-loops, cap CALLS in-degree
- Sentence-transformer encoding (`all-MiniLM-L6-v2`, 384-d) with disk caching
- PyG `HeteroData` construction; edge splitting via `RandomLinkSplit`
  (only CALLS, INHERITS, IMPORTS_MODULE are masked; structural edges are kept)
- Saves: `train_data.pt`, `val_data.pt`, `node_index.json`, `config.pt`

### 3. Training (notebook 02)

- Masked link prediction with BCEWithLogitsLoss
- Auxiliary diversity loss (head orthogonality + variance + coverage)
- Optional auxiliary heads: same-repo prediction, degree-bucket classification
- Early stopping on validation ROC-AUC

### 4. Evaluation (notebook 03)

- Per-edge-type ROC-AUC, Average Precision, Accuracy
- ROC and Precision-Recall curves
- Per-head attention entropy analysis and heatmap visualization

### 5. PR risk assessment (notebooks 04–05)

- **Notebook 04**: Fetches 55 real merged PRs from 13 Django ecosystem repos
  via GitHub API; extracts diffs; prompts GPT-4o with diffs only for baseline
  risk assessment (Severity × Probability framework)
- **Notebook 05**: Loads trained model, computes full node embeddings, extracts
  2-hop attention-weighted subgraphs around changed nodes, and re-prompts
  GPT-4o with diff + structural context. An LLM-as-judge then compares the two
  assessments via fact-checking against the actual codebase.

## Graph schema

### Node types

| Type       | ID pattern                        | Description                       |
|------------|-----------------------------------|-----------------------------------|
| `repo`     | `repo::<name>`                    | Repository root                   |
| `file`     | `file::<name>::<relpath>`         | Python source file                |
| `module`   | `mod::<name>::<dotted.module>`    | Python module                     |
| `class`    | `class::<name>::<module>::<Qual>` | Class definition                  |
| `function` | `func::<name>::<module>::<qual>`  | Function or method definition     |

### Edge types

| Edge                 | Source   | Target   |
|----------------------|----------|----------|
| `CONTAINS_FILE`      | repo     | file     |
| `IMPLEMENTS_MODULE`  | file     | module   |
| `DEFINES_CLASS`      | module   | class    |
| `DEFINES_FUNCTION`   | module   | function |
| `DEFINES_METHOD`     | class    | function |
| `IMPORTS_MODULE`     | module   | module   |
| `INHERITS`           | class    | class    |
| `CALLS`              | function | function |

### Django ecosystem graph

147,234 nodes (13 repo, 12,117 file, 12,117 module, 26,478 class,
81,479 function) and 257,604 canonical edges (490,974 including reverses).

## Programmatic usage

```python
from pathlib import Path
from graph_builder import GraphBuilder

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
