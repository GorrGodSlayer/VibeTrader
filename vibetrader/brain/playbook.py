"""The base brain: curated trading knowledge the trader starts with, before it has any lessons of its own.

Rules live in playbook.json (researched from trading guides, on-chain analytics and security write-ups; every rule
cites its sources). Each prompt gets the rules that matter for its job, ranked by category and by overlap with the
candidate's feature buckets. The concept graph also turns each category into an innate concept that learned
lessons fire synapses to.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("playbook.json")

CATEGORY_LABELS = {
    "rug_detection": "Rug & scam detection",
    "launch_lifecycle": "Launch lifecycle",
    "entry": "Entries",
    "exit": "Exits & trade management",
    "risk": "Risk & sizing",
    "majors": "Trading majors",
    "psychology": "Discipline",
    "market_regime": "Market regime",
}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}

# which categories each job needs
JOB_CATEGORIES = {
    "decide": ("rug_detection", "launch_lifecycle", "entry", "risk", "majors", "market_regime", "psychology"),
    "screen": ("rug_detection", "launch_lifecycle", "entry", "market_regime"),
    "manage": ("exit", "risk", "psychology", "majors"),
    "strategy": tuple(CATEGORY_LABELS),
}


@lru_cache(maxsize=1)
def load() -> tuple[dict[str, Any], ...]:
    if not PATH.exists():
        return ()
    data = json.loads(PATH.read_text(encoding="utf-8"))
    return tuple(r for r in data.get("rules", []) if r.get("rule") and r.get("category") in CATEGORY_LABELS)


def select(job: str, buckets: list[str] | None = None, k: int = 10) -> list[dict[str, Any]]:
    """The k rules most useful for a job: bucket-matched rules first, then the most trusted general ones."""
    cats = JOB_CATEGORIES.get(job, tuple(CATEGORY_LABELS))
    want = set(buckets or [])
    scored = []
    for i, r in enumerate(load()):
        if r["category"] not in cats:
            continue
        applies = set(r.get("applies_to") or [])
        if applies and want and not (applies & want):
            continue  # a bucket-specific rule for a different setup
        score = CONFIDENCE_WEIGHT.get(r.get("confidence"), 0.5) + 1.5 * len(applies & want) - i * 1e-4
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:k]]


def lines(rules: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{r['category']}] {r['rule']}" for r in rules) or "- (no playbook loaded)"


def summary() -> dict[str, Any]:
    rules = load()
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        by_cat.setdefault(r["category"], []).append(r)
    return {
        "count": len(rules),
        "categories": [
            {"key": c, "label": CATEGORY_LABELS[c], "rules": [
                {k: r.get(k) for k in ("id", "rule", "rationale", "confidence", "applies_to", "params", "sources")}
                for r in by_cat.get(c, [])
            ]}
            for c in CATEGORY_LABELS if by_cat.get(c)
        ],
    }
