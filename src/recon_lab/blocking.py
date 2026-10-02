"""Candidate generation: union of four deterministic blocking rules."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

Pair = tuple[str, str]

BLOCKING_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("domain", ("domain_norm",)),
    ("name4_postcode5", ("name4", "postcode5")),
    ("phone10", ("phone10",)),
    ("soundex_state", ("soundex_first", "state_norm")),
]


class Blocker:
    @staticmethod
    def pair(left: str, right: str) -> Pair:
        return (left, right) if left < right else (right, left)

    @staticmethod
    def candidates(records: list[dict[str, object]]) -> tuple[set[Pair], dict[str, int]]:
        """Return the union of pairs over all rules (null keys never block) and per-rule counts."""

        union: set[Pair] = set()
        per_rule: dict[str, int] = {}
        for rule_name, fields in BLOCKING_RULES:
            groups: dict[tuple, list[str]] = defaultdict(list)
            for record in records:
                key = tuple(record.get(f) for f in fields)
                if any(v in (None, "") for v in key):
                    continue
                groups[key].append(str(record["unique_id"]))
            rule_pairs = {Blocker.pair(a, b) for ids in groups.values() for a, b in combinations(sorted(ids), 2)}
            per_rule[rule_name] = len(rule_pairs)
            union |= rule_pairs
        return union, per_rule

    @staticmethod
    def true_pairs(truth: list[dict[str, str]]) -> set[Pair]:
        by_entity: dict[str, list[str]] = defaultdict(list)
        for row in truth:
            by_entity[row["entity_id"]].append(f"{row['org']}:{row['Id']}")
        return {Blocker.pair(a, b) for ids in by_entity.values() for a, b in combinations(sorted(ids), 2)}
