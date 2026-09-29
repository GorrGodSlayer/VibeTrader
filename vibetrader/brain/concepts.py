"""Concept memory: similar lessons are bunched into concepts, and lessons that relate to several concepts
form synapses between them.

Each lesson becomes a sparse TF-IDF vector over its feature buckets, tags, outcome and lesson text. Lessons are
assigned in the order they were learned: a lesson joins the most similar concept if it is close enough, otherwise
it founds a new one. It also fires synapses to the other concepts it resembles, and each firing strengthens that
connection. Everything is deterministic, so the whole brain can be rebuilt from the lessons table at any time.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

JOIN_SIM = 0.34        # min similarity to join an existing concept
SYNAPSE_SIM = 0.16     # min similarity for a lesson to fire a synapse to another concept
MAX_SYNAPSES_PER_LESSON = 3

STOP = set("""a an the and or but if then else of to in on at by for with from as is are was were be been being this that
these those it its into than too very can will just not no nor only own same so such both each few more most other some
any all our your their them they we you he she his her after before above below again further once here there when where
why how do does did doing have has had having i me my myself trade trades token tokens entry entries exit exits price
move moves while under over also still even use using used several held lost level little carried showed real entering
kept keep worked failed paid cost costs gave give given dominated outcome thesis within whenever never always""".split())
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")


def features(lesson: dict[str, Any], outcome: bool = True) -> Counter:
    f: Counter = Counter()
    for b in lesson.get("buckets") or []:
        f[f"b:{b}"] += 1.2
    for t in lesson.get("tags") or []:
        f[f"t:{str(t).lower()}"] += 0.8
    if outcome:
        f["o:win" if (lesson.get("pnl_pct") or 0) > 0 else "o:loss"] += 1.0
    text = " ".join(str(lesson.get(k) or "") for k in ("lesson", "what_worked", "what_failed")).lower()
    for w in WORD.findall(text):
        if w not in STOP and not w.isdigit():
            f[f"w:{w}"] += 1.0
    return f


def pretty(term: str) -> str:
    kind, _, val = term.partition(":")
    if kind == "o":
        return "winners" if val == "win" else "losers"
    return val


@dataclass
class Concept:
    id: int
    centroid: dict[str, float] = field(default_factory=dict)
    norm: float = 0.0
    members: list[int] = field(default_factory=list)
    wins: int = 0
    pnl_sum: float = 0.0
    first_ts: str | None = None
    last_ts: str | None = None
    prior: bool = False                     # an innate playbook concept (base knowledge, not learned from trades)
    category: str | None = None
    rules: list[dict[str, Any]] = field(default_factory=list)

    def add(self, vec: dict[str, float]) -> None:
        for k, v in vec.items():
            self.centroid[k] = self.centroid.get(k, 0.0) + v
        self.norm = math.sqrt(sum(v * v for v in self.centroid.values()))

    def sim(self, vec: dict[str, float]) -> float:
        if not self.norm:
            return 0.0
        return sum(v * self.centroid.get(k, 0.0) for k, v in vec.items()) / self.norm


class ConceptGraph:
    def __init__(self) -> None:
        self.df: Counter = Counter()
        self.n_docs = 0
        self.concepts: list[Concept] = []
        self.synapses: dict[tuple[int, int], dict[str, Any]] = {}  # (a, b) with a < b -> {weight, created, last}
        self.assignments: dict[int, dict[str, Any]] = {}            # lesson id -> {concept, new_concept, links}
        self.vectors: dict[int, dict[str, float]] = {}

    # ------------------------------------------------------------ learning
    @classmethod
    def build(cls, lessons: list[dict[str, Any]], priors: tuple[dict[str, Any], ...] | list[dict[str, Any]] = ()) -> ConceptGraph:
        """Innate concepts from the playbook first (one per category), then every lesson in the order it was learned."""
        g = cls()
        prior_feats: dict[str, list[Counter]] = {}
        for r in priors:
            doc = {"lesson": f"{r.get('rule', '')} {r.get('rationale', '')}", "tags": [r["category"]],
                   "buckets": r.get("applies_to") or []}
            prior_feats.setdefault(r["category"], []).append(features(doc, outcome=False))
        feats = [(l, features(l)) for l in lessons]
        for f in [*[x for fs in prior_feats.values() for x in fs], *(f for _, f in feats)]:
            g.df.update(f.keys())  # global document frequencies first, so early lessons are weighted like late ones
        g.n_docs = sum(len(fs) for fs in prior_feats.values()) + len(feats)
        for cat, fs in prior_feats.items():
            c = Concept(id=len(g.concepts) + 1, prior=True, category=cat,
                        rules=[{"id": r.get("id"), "rule": r["rule"]} for r in priors if r["category"] == cat])
            for f in fs:
                c.add(g._vector(f))
            g.concepts.append(c)
        for l, f in feats:
            g._assign(l, f)
        return g

    def add(self, lesson: dict[str, Any]) -> dict[str, Any]:
        """Learn one new lesson; returns what happened (concept joined or founded, synapses fired)."""
        f = features(lesson)
        self.df.update(f.keys())
        self.n_docs += 1
        return self._assign(lesson, f)

    def _vector(self, f: Counter) -> dict[str, float]:
        vec = {k: tf * (math.log((1 + self.n_docs) / (1 + self.df[k])) + 1) for k, tf in f.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {k: v / norm for k, v in vec.items()}

    def _assign(self, lesson: dict[str, Any], f: Counter) -> dict[str, Any]:
        vec = self._vector(f)
        sims = sorted(((c.sim(vec), c) for c in self.concepts), key=lambda x: x[0], reverse=True)
        learned = [(s, c) for s, c in sims if not c.prior]  # lessons join learned concepts, never the playbook
        if learned and learned[0][0] >= JOIN_SIM:
            home, new = learned[0][1], False
        else:
            home, new = Concept(id=len(self.concepts) + 1), True
            self.concepts.append(home)
        others = [(s, c) for s, c in sims if c is not home]
        home.add(vec)
        home.members.append(lesson["id"])
        home.wins += int((lesson.get("pnl_pct") or 0) > 0)
        home.pnl_sum += lesson.get("pnl_pct") or 0
        home.first_ts = home.first_ts or lesson.get("ts")
        home.last_ts = lesson.get("ts")

        links = []
        for s, c in others[:MAX_SYNAPSES_PER_LESSON]:
            if s < SYNAPSE_SIM:
                break
            key = (min(home.id, c.id), max(home.id, c.id))
            syn = self.synapses.setdefault(key, {"weight": 0, "created": lesson["id"]})
            syn["weight"] += 1
            syn["last"] = lesson["id"]
            links.append({"concept": c.id, "sim": round(s, 3), "weight": syn["weight"], "new": syn["weight"] == 1})
        out = {"concept": home.id, "new_concept": new, "links": links}
        self.assignments[lesson["id"]] = out
        self.vectors[lesson["id"]] = vec
        return out

    # ------------------------------------------------------------ reading
    def label(self, c: Concept, n: int = 3) -> str:
        """Strongest feature bucket first (the market condition), then the words that set the concept apart."""
        if c.prior:
            from vibetrader.brain.playbook import CATEGORY_LABELS

            return f"playbook · {CATEGORY_LABELS.get(c.category or '', c.category)}"
        ranked = sorted(c.centroid.items(), key=lambda kv: kv[1], reverse=True)
        picked: list[str] = [pretty(t) for t, _ in ranked if t.startswith("b:")][:1]
        for t, _ in ranked:
            if not t.startswith(("w:", "t:")):
                continue
            p = pretty(t)
            if not any(p in q or q in p for q in picked):
                picked.append(p)
            if len(picked) == n:
                break
        return " · ".join(picked) or f"concept {c.id}"

    def concept_view(self, c: Concept) -> dict[str, Any]:
        n = len(c.members)
        lead = max(c.members, key=lambda i: c.sim(self.vectors.get(i, {})), default=None)
        top = sorted(c.centroid.items(), key=lambda kv: kv[1], reverse=True)[:8]
        return {
            "id": c.id, "label": self.label(c), "n": n, "wins": c.wins, "losses": n - c.wins,
            "win_rate": round(c.wins / n, 3) if n else 0, "avg_pnl_pct": round(c.pnl_sum / n, 2) if n else 0,
            "terms": [pretty(t) for t, _ in top], "lead": lead, "first_ts": c.first_ts, "last_ts": c.last_ts,
            "prior": c.prior, "category": c.category, "rules": c.rules,
        }

    def concept(self, cid: int) -> Concept | None:
        return self.concepts[cid - 1] if 0 < cid <= len(self.concepts) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "concepts": [self.concept_view(c) for c in self.concepts],
            "synapses": [{"a": a, "b": b, **v} for (a, b), v in self.synapses.items()],
            "assignments": self.assignments,
        }

    def relevant(self, buckets: list[str], k: int = 4, min_members: int = 2) -> list[dict[str, Any]]:
        """Concepts that match a candidate's feature buckets, for the decision prompt."""
        vec = self._vector(Counter({f"b:{b}": 1.2 for b in buckets}))
        scored = sorted(((c.sim(vec), c) for c in self.concepts if not c.prior and len(c.members) >= min_members),
                        key=lambda x: x[0], reverse=True)
        return [{**self.concept_view(c), "match": round(s, 3)} for s, c in scored[:k] if s > 0]
