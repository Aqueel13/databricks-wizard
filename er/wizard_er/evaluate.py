"""Pairwise evaluation against the generator's answer key.

A pair of source records is a true match when both map to the same true_supplier_id. Precision, recall and F1
are computed over all record pairs, overall and per script pair (e.g. cyrillic-latin), so weak spots in
cross-language matching are visible instead of averaged away.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from itertools import combinations

PL = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
CYR = re.compile("[Ѐ-ӿ]")


def script(name: str | None) -> str:
    n = name or ""
    return "cyrillic" if CYR.search(n) else "polish" if PL.search(n) else "latin"


def _pairs(groups: dict[str, list]) -> set[tuple[str, str]]:
    out = set()
    for ms in groups.values():
        if len(ms) > 1:
            out.update(tuple(sorted(p)) for p in combinations(ms, 2))
    return out


def pairwise(pred: dict[str, str], truth: dict[str, str], names: dict[str, str]) -> dict:
    ids = [u for u in pred if u in truth]
    pg, tg = defaultdict(list), defaultdict(list)
    for u in ids:
        pg[pred[u]].append(u)
        tg[truth[u]].append(u)
    P, T = _pairs(pg), _pairs(tg)
    cat = lambda p: "-".join(sorted((script(names.get(p[0])), script(names.get(p[1])))))  # noqa: E731
    tp, fp, fn = Counter(), Counter(), Counter()
    for p in P & T:
        tp[cat(p)] += 1
    for p in P - T:
        fp[cat(p)] += 1
    for p in T - P:
        fn[cat(p)] += 1

    def m(t, f, n):
        pr = t / (t + f) if t + f else 1.0
        rc = t / (t + n) if t + n else 1.0
        return {"tp": t, "fp": f, "fn": n, "precision": round(pr, 4), "recall": round(rc, 4),
                "f1": round(2 * pr * rc / (pr + rc), 4) if pr + rc else 0.0}

    # B-cubed: per-record precision/recall of its cluster, averaged (each record counts once, so a few
    # very large clusters cannot dominate the score the way they dominate pair counts)
    pc = {u: set(pg[pred[u]]) for u in ids}
    tc = {u: set(tg[truth[u]]) for u in ids}
    bp = sum(len(pc[u] & tc[u]) / len(pc[u]) for u in ids) / len(ids)
    br = sum(len(pc[u] & tc[u]) / len(tc[u]) for u in ids) / len(ids)
    out = {"overall": m(len(P & T), len(P - T), len(T - P)), "records": len(ids),
           "bcubed": {"precision": round(bp, 4), "recall": round(br, 4), "f1": round(2 * bp * br / (bp + br), 4)}}
    for c in sorted(set(tp) | set(fp) | set(fn)):
        out[c] = m(tp[c], fp[c], fn[c])
    return out, sorted(P - T), sorted(T - P)
