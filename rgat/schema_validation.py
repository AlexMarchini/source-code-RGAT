"""
rgat.schema_validation – Strict per-node-type feature validation.

Verifies that **every** node carries the expected feature keys with the
correct types and consistent dimensionality.  Raises immediately on any
inconsistency — there are no silent fallbacks or automatic embeddings for
missing features.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set

from rgat.config import REQUIRED_SCALAR_FEATURES


# Types that are acceptable as numeric scalars in feature dicts.
_NUMERIC_TYPES = (int, float, bool)


def validate_features(nodes: List[Dict[str, Any]]) -> None:
    """Validate feature completeness and type consistency for every node.

    Parameters
    ----------
    nodes : list[dict]
        Raw node dicts as returned by :func:`rgat.data_loading.load_json`.

    Raises
    ------
    ValueError
        If any node is missing a required feature key, has an unexpected
        value type, has an empty ``embedding_input``, or is missing the
        ``leiden_community`` integer.
    """
    errors: List[str] = []
    empty_embedding_counts: Counter = Counter()

    # Track per-type feature-key sets to detect inconsistencies across nodes
    # of the same type.
    type_key_sets: Dict[str, Set[frozenset]] = defaultdict(set)

    for i, node in enumerate(nodes):
        nid: str = node["id"]
        ntype: str = node["type"]
        feats: Dict[str, Any] = node.get("features", {})

        required_keys = REQUIRED_SCALAR_FEATURES.get(ntype)
        if required_keys is None:
            errors.append(
                f"nodes[{i}] (id={nid!r}): no feature schema defined for "
                f"node type {ntype!r}"
            )
            continue

        # ── 1. Check required scalar / boolean features ────────────────
        for key in required_keys:
            if key not in feats:
                errors.append(
                    f"[{ntype}] node {nid!r}: missing required feature '{key}'"
                )
                continue
            val = feats[key]
            if val is None:
                errors.append(
                    f"[{ntype}] node {nid!r}: feature '{key}' is None"
                )
            elif not isinstance(val, _NUMERIC_TYPES):
                errors.append(
                    f"[{ntype}] node {nid!r}: feature '{key}' should be "
                    f"numeric, got {type(val).__name__} = {val!r}"
                )

        # ── 2. Check embedding_input ───────────────────────────────────
        if "embedding_input" not in feats:
            errors.append(
                f"[{ntype}] node {nid!r}: missing 'embedding_input'"
            )
        else:
            ei = feats["embedding_input"]
            if not isinstance(ei, str):
                errors.append(
                    f"[{ntype}] node {nid!r}: 'embedding_input' must be a "
                    f"string, got {type(ei).__name__} = {ei!r}"
                )
            elif not ei.strip():
                # Empty string — will be replaced by qualname from node ID
                # during text encoding.  Track for warning but don't fail.
                empty_embedding_counts[ntype] += 1

        # ── 3. Check leiden_community ──────────────────────────────────
        if "leiden_community" not in feats:
            errors.append(
                f"[{ntype}] node {nid!r}: missing 'leiden_community'"
            )
        else:
            lc = feats["leiden_community"]
            if not isinstance(lc, int):
                errors.append(
                    f"[{ntype}] node {nid!r}: 'leiden_community' must be "
                    f"int, got {type(lc).__name__} = {lc!r}"
                )

        # Track the set of feature keys for consistency check
        type_key_sets[ntype].add(frozenset(feats.keys()))

    # ── 4. Cross-node consistency: same keys for all nodes of a type ───
    for ntype, key_variants in type_key_sets.items():
        if len(key_variants) > 1:
            # Find the symmetric differences
            key_list = list(key_variants)
            reference = key_list[0]
            for variant in key_list[1:]:
                extra = variant - reference
                missing = reference - variant
                if extra or missing:
                    errors.append(
                        f"[{ntype}] inconsistent feature keys across nodes. "
                        f"Some nodes have extra keys {sorted(extra)} "
                        f"or are missing keys {sorted(missing)}"
                    )
                    break  # one report per type is enough

    # ── Report ─────────────────────────────────────────────────────────
    if errors:
        header = (
            f"Feature validation failed with {len(errors)} error(s).\n"
            f"First 20 errors:\n"
        )
        detail = "\n".join(f"  • {e}" for e in errors[:20])
        if len(errors) > 20:
            detail += f"\n  … and {len(errors) - 20} more error(s)"
        raise ValueError(header + detail)

    # Warn about empty embedding_input strings
    if empty_embedding_counts:
        print(
            f"[schema_validation] WARNING: {sum(empty_embedding_counts.values())} "
            f"node(s) have empty 'embedding_input'. "
            f"Their node-ID qualname will be used as fallback text.",
            file=sys.stderr,
        )
        for ntype, cnt in empty_embedding_counts.most_common():
            print(f"  {ntype}: {cnt:,} empty", file=sys.stderr)

    # Quick summary
    type_counts = Counter(n["type"] for n in nodes)
    print("[schema_validation] Feature validation passed for all nodes:")
    for ntype in sorted(type_counts):
        n_scalar = len(REQUIRED_SCALAR_FEATURES.get(ntype, []))
        print(
            f"  {ntype:>10s}: {type_counts[ntype]:>7,} nodes × "
            f"{n_scalar} scalar features + embedding_input + leiden_community"
        )
