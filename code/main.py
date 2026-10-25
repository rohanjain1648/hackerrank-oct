"""Ghost Data deletion-audit agent - command-line entry point.

    python code/main.py                     # audit dataset/deletion_requests.csv -> ./output.csv
    python code/main.py --split sample      # audit the 32 public samples and score them
    python code/main.py --llm               # also use Claude (needs ANTHROPIC_API_KEY)

Every run writes a per-case reasoning trace to .cache/runs/<split>_trace.json
and a usage summary (model calls, tokens, cost) used by
code/evaluation/usage_report.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_agent.agent import DeletionAuditAgent, load_requests, write_output  # noqa: E402
from audit_agent.llm import ClaudeClient  # noqa: E402
from audit_agent.paths import DATASET_DIR, OUTPUT_CSV, RUNS_DIR  # noqa: E402
from audit_agent.usage import UsageTracker  # noqa: E402


def validate(path: Path, expected_ids: list[str]) -> list[str]:
    """Contract checks on the produced CSV (header, rows, JSON, numbering, phases)."""
    import csv

    from audit_agent.policy import OPERATION_ORDER, OPERATION_STATE, PHASES

    problems = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    if header != ["case_id", "deletion_status", "steps_json"]:
        problems.append(f"bad header {header}")
    ids = [r[0] for r in rows]
    if ids != expected_ids:
        problems.append("case ids/order differ from the request file")
    for cid, status, raw in rows:
        steps = json.loads(raw)
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            problems.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            problems.append(f"{cid}: NOT_APPLICABLE with steps")
        if status != "NOT_APPLICABLE" and not steps:
            problems.append(f"{cid}: deletion case with an empty plan")
        last = (-1, -1)
        for i, s in enumerate(steps, 1):
            if s["step_no"] != i:
                problems.append(f"{cid}: step numbering")
            pos = (PHASES.index(s["phase"]), OPERATION_ORDER[s["phase"]].index(s["operation"]))
            if pos < last:
                problems.append(f"{cid}: phase/operation order")
            last = pos
            if OPERATION_STATE[s["operation"]] != s["state"]:
                problems.append(f"{cid}: state mismatch")
            if s["phase"] == "REMEDIATION" and s["evidence_id"]:
                problems.append(f"{cid}: remediation cites evidence")
            if status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
                problems.append(f"{cid}: COMPLETE step without verification evidence")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=["eval", "sample"], default="eval")
    ap.add_argument("--output", type=Path, default=None, help="output CSV (default ./output.csv for eval)")
    ap.add_argument("--llm", action="store_true", help="use Claude for intent and screenshots")
    ap.add_argument("--model", default=None, help="override the Claude model id")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    started = time.time()
    usage = UsageTracker()
    llm = ClaudeClient(usage=usage, model=args.model) if args.llm else None
    if llm is not None and not llm.enabled:
        log("[llm] ANTHROPIC_API_KEY not set - continuing with the deterministic/offline path")
    agent = DeletionAuditAgent(DATASET_DIR, llm=llm, usage=usage, log=log)

    sample = load_requests(DATASET_DIR / "sample_deletion_requests.csv")
    evals = load_requests(DATASET_DIR / "deletion_requests.csv")
    target = sample if args.split == "sample" else evals
    results = agent.run(target, context_requests=sample + evals)

    out = args.output or (OUTPUT_CSV if args.split == "eval" else RUNS_DIR / "sample_predictions.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_output(results, out)
    problems = validate(out, [r.case_id for r in sorted(target, key=lambda r: int(r.case_id.split("_")[-1]))])

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = RUNS_DIR / f"{args.split}_trace.json"
    trace_path.write_text(json.dumps({r.case_id: {"status": r.status, **r.trace} for r in results},
                                     indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    elapsed = time.time() - started
    summary = usage.summary(requests=len(target), seconds=elapsed, split=args.split,
                            media_engines=agent.store.media_engines)
    (RUNS_DIR / f"{args.split}_usage.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log(f"[done] {len(results)} cases -> {out}  statuses={counts}  {elapsed:.1f}s")
    log(f"[done] trace: {trace_path}")
    if problems:
        log("[validate] PROBLEMS:\n  " + "\n  ".join(problems[:40]))
    else:
        log("[validate] output passes all contract checks")

    if args.split == "sample":
        sys.path.insert(0, str(Path(__file__).resolve().parent / "evaluation"))
        from metrics import load_rows, score

        m = score(load_rows(out), load_rows(DATASET_DIR / "sample_output.csv"))
        log(json.dumps({k: v for k, v in m.items() if k != "per_case"}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
