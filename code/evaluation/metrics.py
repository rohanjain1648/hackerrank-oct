"""Scoring of predicted rows against reference rows.

Metrics (all macro over cases unless stated):

* status accuracy - exact ``deletion_status`` match;
* step precision / recall / F1 - multiset match of full step tuples
  (phase, system, resource, operation, state, evidence), micro-averaged;
  also reported without ``evidence_id`` ("core") to separate discovery from
  citation errors;
* field accuracy - steps aligned by position, per-field agreement;
* sequence accuracy - the whole ``steps_json`` array equal, in order;
* order similarity - longest-common-subsequence ratio of the step lists.
"""

from __future__ import annotations

import csv
import json
from collections import Counter

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load_rows(path) -> dict[str, tuple[str, list[dict]]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def _key(step, with_evidence=True):
    fields = FIELDS if with_evidence else FIELDS[:-1]
    return tuple(step.get(f, "") for f in fields)


def _lcs(a, b) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def score(pred: dict, gold: dict) -> dict:
    cases = sorted(gold)
    tp = fp = fn = tp_core = fp_core = fn_core = 0
    field_hits = Counter()
    field_total = 0
    status_ok = seq_ok = 0
    order_sim = 0.0
    per_case = []
    for cid in cases:
        g_status, g_steps = gold[cid]
        p_status, p_steps = pred.get(cid, ("MISSING", []))
        status_ok += p_status == g_status
        seq = [_key(s) for s in p_steps] == [_key(s) for s in g_steps]
        seq_ok += seq
        gk, pk = Counter(_key(s) for s in g_steps), Counter(_key(s) for s in p_steps)
        inter = sum((gk & pk).values())
        tp += inter
        fp += sum(pk.values()) - inter
        fn += sum(gk.values()) - inter
        gc, pc = Counter(_key(s, False) for s in g_steps), Counter(_key(s, False) for s in p_steps)
        ic = sum((gc & pc).values())
        tp_core += ic
        fp_core += sum(pc.values()) - ic
        fn_core += sum(gc.values()) - ic
        n = max(len(g_steps), len(p_steps))
        field_total += n
        for i in range(n):
            if i < len(g_steps) and i < len(p_steps):
                for f in FIELDS:
                    field_hits[f] += g_steps[i].get(f) == p_steps[i].get(f)
        a, b = [_key(s) for s in p_steps], [_key(s) for s in g_steps]
        sim = 1.0 if not a and not b else 2 * _lcs(a, b) / (len(a) + len(b))
        order_sim += sim
        per_case.append({"case_id": cid, "gold_status": g_status, "pred_status": p_status,
                         "status_ok": p_status == g_status, "sequence_ok": seq,
                         "missing": sorted(set(gk) - set(pk)), "extra": sorted(set(pk) - set(gk))})
    n_cases = len(cases) or 1

    def prf(t, p_, n_):
        prec = t / (t + p_) if t + p_ else 1.0
        rec = t / (t + n_) if t + n_ else 1.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        return round(prec, 4), round(rec, 4), round(f1, 4)

    return {
        "cases": len(cases),
        "status_accuracy": round(status_ok / n_cases, 4),
        "step_precision_recall_f1": prf(tp, fp, fn),
        "core_step_precision_recall_f1": prf(tp_core, fp_core, fn_core),
        "field_accuracy": {f: round(field_hits[f] / field_total, 4) if field_total else 1.0 for f in FIELDS},
        "sequence_accuracy": round(seq_ok / n_cases, 4),
        "order_similarity": round(order_sim / n_cases, 4),
        "per_case": per_case,
    }
