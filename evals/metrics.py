"""Retrieval metrics and the paired bootstrap, in the standard library (S1-18; ticket 72 E, ticket 17's
adoption rule). The tier 1 runner (evals/tier1.py) uses them, and S1-36's tier 2 can import them.

A ranking is a list of ids, best first. `relevant` maps an id to its graded relevance (above zero is
relevant), or is a set of relevant ids (each 1).
"""

import math
import random


def _grades(relevant):
    return relevant if isinstance(relevant, dict) else {r: 1 for r in relevant}


def recall_at(ranking, relevant, k):
    """The share of the relevant ids found in the top k (None when nothing is relevant)."""
    grades = {r for r, g in _grades(relevant).items() if g > 0}
    if not grades:
        return None
    return len(grades & set(ranking[:k])) / len(grades)


def ndcg_at(ranking, relevant, k=10):
    """nDCG@k with linear gain (trec_eval's and so BEIR's): the sum of grade / log2(rank + 1) over the
    top k, divided by the same for the best possible order of the relevant ids."""
    grades = {r: g for r, g in _grades(relevant).items() if g > 0}
    if not grades:
        return None
    dcg = sum(grades.get(r, 0) / math.log2(i + 2) for i, r in enumerate(ranking[:k]))
    ideal = sum(g / math.log2(i + 2) for i, g in enumerate(sorted(grades.values(), reverse=True)[:k]))
    return dcg / ideal


def mrr_at(ranking, relevant, k=10):
    """1 / the rank of the first relevant id in the top k, or 0."""
    grades = {r for r, g in _grades(relevant).items() if g > 0}
    if not grades:
        return None
    return next((1 / (i + 1) for i, r in enumerate(ranking[:k]) if r in grades), 0.0)


def documents(passages, document_of):
    """A ranking of passages collapsed to documents: each document at the rank of its best passage
    (MaxP). document_of(passage) gives the passage's document ids (several when documents share a
    file), in order."""
    ranked, seen = [], set()
    for passage in passages:
        for doc in document_of(passage):
            if doc not in seen:
                seen.add(doc)
                ranked.append(doc)
    return ranked


def mean(values):
    return sum(values) / len(values) if values else None


def bootstrap(values, groups=None, resamples=10_000, seed=0, level=0.95):
    """A percentile bootstrap interval for the mean of values (paired: pass per-query differences).
    With groups (one label per value), whole groups are resampled, as QASPER's questions are by paper
    (ticket 72 E). Deterministic for a given seed. Returns (low, high), or None with no values."""
    if not values:
        return None
    groups = list(range(len(values))) if groups is None else groups
    totals = {}
    for value, group in zip(values, groups, strict=True):
        total, count = totals.get(group, (0.0, 0))
        totals[group] = (total + value, count + 1)
    clusters = [totals[g] for g in sorted(totals)]
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total = count = 0
        for t, c in rng.choices(clusters, k=len(clusters)):
            total += t
            count += c
        means.append(total / count)
    means.sort()
    tail = (1 - level) / 2
    return means[int(math.floor(tail * (resamples - 1)))], means[int(math.ceil((1 - tail) * (resamples - 1)))]
