"""Evaluation workflow.

    python code/evaluation/main.py            # writes evaluation_report.md and usage_report.md

1. Scores the full agent on the 32 public samples (status, step P/R/F1,
   field, sequence and order metrics).
2. Ablations: re-runs the linker with each workflow-linking signal disabled.
3. Runs the participant-authored custom cases (custom_cases.py).
4. Profiles the predictions for the 518 evaluation requests (no labels exist,
   so this checks plausibility: statuses, step mix, how resources were linked,
   contract validation).
5. Renders usage_report.md from the usage summary of the last full-dataset
   run (written by ``python code/main.py``).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

from audit_agent import linker  # noqa: E402
from audit_agent.agent import DeletionAuditAgent, load_requests  # noqa: E402
from audit_agent.paths import DATASET_DIR, OUTPUT_CSV, RUNS_DIR  # noqa: E402
from custom_cases import REQUESTS as CUSTOM_REQUESTS, run_custom_cases  # noqa: E402
from metrics import load_rows, score  # noqa: E402

# Development history on the public samples (measured with this harness after
# each change; see log.txt for the session that produced them).
HISTORY = [
    ("v0", "Direct identifier links + global assignment of identifier-less profiles/analytics, naive tie-break",
     "1.000", "0.953", "0.719"),
    ("v1", "Hard workflow-timing window (profiles 25-95 min before reported_at, single-request workflows +/-15 min); "
           "first account-table snapshot as absence proof for single-request cases", "1.000", "0.961", "0.719"),
    ("v2", "Coverage semantics: a snapshot proves absence only when it holds the subject's records; coverage counts "
           "as evidence-backed absence when scoring COMPLETE cases", "1.000", "0.971", "0.781"),
    ("v3", "Workflow claim consistency: profile-submission result mirrors the reported status "
           "(COMPLETE~SUCCESS, PARTIALLY_COMPLETE~PARTIAL, NOT_STARTED~FAILED)", "1.000", "0.992", "0.906"),
    ("v4", "Eval-set audit fixes (no sample change): ownership guard for identifier-less pool, document-level PII "
           "exposure for screenshots, catalogue context rows, removed 'single candidate' forcing (found by custom_06)",
           "1.000", "0.992", "0.906"),
]

FAILURES = [
    ("Intent: 3 sample follow-ups classified NOT_APPLICABLE (\"confirm the profile is gone\", \"have already been removed\", "
     "\"remove that search result\")", "Added follow-up/verification patterns; negation-aware keep-account cues keep the 10 "
     "real non-deletion requests out (\"don't delete\", \"not asking for deletion\", \"marketing lists only\")."),
    ("Old observations of the same person (an earlier March deletion of user_17) leaked into the September case",
     "Direct links only count inside the request's life-cycle (requested_at .. audit_at)."),
    ("Profiles that only ever answer 404 and anonymised analytics carry no identifier; several cases share each "
     "workflow window", "Global assignment of deletion-worker activities: hard timing window, claim consistency "
     "(result vs reported status), evidence affinity, policy-action cadence (7:29 / 6:30 alternating gaps), then "
     "elimination."),
    ("Evidence captured after audit_at (e.g. a 09-17 snapshot showing an account ACTIVE for a 09-15 audit)",
     "State and citations use only files captured <= audit_at; later files may only identify a resource."),
    ("A replica-only profile (primary deleted, read replica still serving it)", "Every table of a SQL snapshot is a "
     "separate copy; any surviving copy makes the record PRESENT."),
    ("Catalogue links are claims; a first version rejected any link whose screen showed identifiers not belonging "
     "to the subject, which left case_63 (marketing profile) COMPLETE with an empty plan - impossible under the "
     "contract", "A link is rejected only when the record shows identifiers of a *different known* person; "
     "identifiers that resolve to nobody cannot contradict it. Final audit also checks no deletion case is empty."),
    ("Story screenshot: support ticket judged CLEAN because name/e-mail were on a different OCR row",
     "Document-level exposure for the catalogued record of a screen."),
    ("analytics_7438 (still identifiable for Omar Darwish) assigned from the identifier-less pool to another case",
     "Resources that any observation ties to a known person never enter that pool for someone else."),
    ("custom_06: a case with a single candidate profile grabbed another case's better-fitting profile",
     "Removed case-forced assignment; best-margin assignment decides."),
]

UNRECOVERABLE = (
    "Three sample steps reference resources that occur in no participant-visible file (account_004, "
    "audit_record_6645, audit_record_5569); they bound recall at 0.985 on the samples."
)


def fmt(x):
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def run_sample(agent, sample, context):
    results = agent.run(sample, context_requests=context)
    pred = {r.case_id: (r.status, [s.as_dict(i + 1) for i, s in enumerate(r.steps)]) for r in results}
    return score(pred, load_rows(DATASET_DIR / "sample_output.csv")), results


def eval_profile():
    rows = load_rows(OUTPUT_CSV)
    trace = json.loads((RUNS_DIR / "eval_trace.json").read_text(encoding="utf-8"))
    statuses = Counter(s for s, _ in rows.values())
    ops = Counter(st["operation"] for _, steps in rows.values() for st in steps)
    links = Counter(d["link"] for t in trace.values() for d in t.get("decisions", []))
    basis = Counter(d["state"]["basis"] for t in trace.values() for d in t.get("decisions", []))
    no_account = [c for c, t in trace.items() if t["status"] != "NOT_APPLICABLE"
                  and not any(d["resource"].startswith("account_") for d in t.get("decisions", []))]
    n_steps = [len(steps) for _, steps in rows.values()]
    return {"cases": len(rows), "statuses": dict(statuses), "operations": dict(ops), "links": dict(links),
            "basis": dict(basis), "no_account": no_account, "avg_steps": sum(n_steps) / max(len(n_steps), 1)}


def render_report(full, ablations, custom, profile) -> str:
    L = []
    L.append("# Evaluation report - Ghost Data deletion-audit agent\n")
    L.append("Generated by `python code/evaluation/main.py`. Reference labels exist only for the 32 public samples "
             "(`dataset/sample_output.csv`); the 518 evaluation requests are checked for plausibility and contract "
             "validity. Metrics are defined in `code/evaluation/metrics.py`.\n")
    L.append("## 1. Sample-set accuracy (32 labelled cases)\n")
    p, r, f = full["step_precision_recall_f1"]
    cp, cr, cf = full["core_step_precision_recall_f1"]
    L.append("| Metric | Value |\n|---|---|")
    L.append(f"| Case-level deletion-status accuracy | {fmt(full['status_accuracy'])} |")
    L.append(f"| Step precision / recall / F1 (all 6 fields incl. evidence_id) | {p:.3f} / {r:.3f} / {f:.3f} |")
    L.append(f"| Step precision / recall / F1 without evidence_id | {cp:.3f} / {cr:.3f} / {cf:.3f} |")
    L.append(f"| Sequence accuracy (whole steps_json identical, in order) | {fmt(full['sequence_accuracy'])} |")
    L.append(f"| Order similarity (LCS ratio of step lists) | {fmt(full['order_similarity'])} |")
    L.append("\n**Field-level accuracy** (steps aligned by position):\n")
    L.append("| " + " | ".join(full["field_accuracy"]) + " |")
    L.append("|" + "---|" * len(full["field_accuracy"]))
    L.append("| " + " | ".join(fmt(v) for v in full["field_accuracy"].values()) + " |")
    bad = [c for c in full["per_case"] if c["missing"] or c["extra"] or not c["status_ok"]]
    L.append("\n**Remaining sample errors**\n")
    for c in bad:
        L.append(f"- `{c['case_id']}`: missing {[m[2] + ' ' + m[3] for m in c['missing']]}, "
                 f"extra {[m[2] + ' ' + m[3] for m in c['extra']]}")
    L.append(f"\n{UNRECOVERABLE}\n")

    L.append("## 2. Ablations (sample set)\n")
    L.append("Each row disables one signal used to tie identifier-less records to a request.\n")
    L.append("| Configuration | Status acc. | Step F1 | Sequence acc. |\n|---|---|---|---|")
    for name, m in ablations:
        L.append(f"| {name} | {fmt(m['status_accuracy'])} | {m['step_precision_recall_f1'][2]:.3f} | "
                 f"{fmt(m['sequence_accuracy'])} |")

    L.append("\n## 3. Additional evaluation cases created for this solution\n")
    L.append("`code/evaluation/custom_cases.py` writes a fictional mini-dataset in the real formats (SQL replica, "
             "trace-joined log, JSON service export, hand-over text, inventory SQL, activity log) and runs the agent "
             "end-to-end. Expected answers were derived by hand from the policy.\n")
    L.append("| Case | What it tests | Expected | Got | Steps exact |\n|---|---|---|---|---|")
    purpose = {
        "custom_01": "historical e-mail + replica-only profile (drift) + search entry keyed by old address",
        "custom_02": "look-alike decoy (`ben.jr@...`) must be ignored; 404 proven by trace-joined lookup",
        "custom_03": "a capture after audit_at says the connector is active again - must be ignored",
        "custom_04": "exposed account tombstone -> REDACT, keep stable user_id",
        "custom_05": "negated request (\"don't delete anything\") -> NOT_APPLICABLE",
        "custom_06": "restorable backup; identifier-less profile won through workflow-activity assignment",
    }
    for c in custom:
        L.append(f"| {c['case_id']} | {purpose[c['case_id']]} | {c['expected_status']} | {c['status']} | "
                 f"{'yes' if c['steps_ok'] else 'NO'} |")
    passed = sum(c["status_ok"] and c["steps_ok"] for c in custom)
    L.append(f"\n{passed}/{len(custom)} custom cases reproduce the expected status and the exact step list.\n")
    texts = "; ".join(f"`{cid}`: \"{t}\"" for cid, _, _, t in CUSTOM_REQUESTS[:2])
    L.append(f"Example request texts: {texts}.\n")

    L.append("## 4. Evaluation-set profile (518 requests, no labels)\n")
    L.append(f"- Status distribution: {profile['statuses']}")
    L.append(f"- Average steps per case: {profile['avg_steps']:.2f}")
    L.append(f"- How resources were linked: {profile['links']} "
             "(direct = identifier in evidence; workflow = deletion-worker activity assignment; hint = later evidence)")
    L.append(f"- State basis: {profile['basis']} (explicit observation / snapshot coverage / target state assumed)")
    L.append(f"- Operations emitted: {profile['operations']}")
    L.append(f"- Deletion cases without a profile step ({len(profile['no_account'])}): {profile['no_account']} - "
             "single-request (screenshot/PDF) cases whose profile id appears in no participant-visible file.")
    L.append("- `python code/main.py` validates the file contract on every run (header, 518 sorted rows, JSON, "
             "numbering, phase/operation order, state/operation consistency, empty evidence on remediation, "
             "evidence on every COMPLETE verification).\n")

    L.append("## 5. Failures found and improvements made\n")
    L.append("| # | Failure found | Improvement |\n|---|---|---|")
    for i, (a, b) in enumerate(FAILURES, 1):
        L.append(f"| {i} | {a} | {b} |")
    L.append("\n**Development history on the sample set**\n")
    L.append("| Version | Change | Status acc. | Step F1 | Sequence acc. |\n|---|---|---|---|---|")
    for v in HISTORY:
        L.append(f"| {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[4]} |")
    L.append("\n## 6. Known limitations\n")
    L.append("- When several requests share one workflow window and their identifier-less records are rendered "
             "identically, the assignment falls back to a deterministic tie-break (earliest activity -> lowest "
             "case number); such ties are reported in the per-case trace (`.cache/runs/eval_trace.json`).")
    L.append("- Single-request story cases depend on screenshot transcription. The offline path uses RapidOCR; "
             "with `--llm` Claude vision transcribes the screens into the same row format.")
    L.append("- The sample set is small (32 cases), so the workflow-linking signals were checked against the "
             "evaluation set's structure (batch evidence files, status mix) and the custom cases as well.")
    return "\n".join(L) + "\n"


def render_usage(summary: dict) -> str:
    L = ["# Usage report - final full-dataset run\n"]
    L.append(f"Run that produced `output.csv`: split `{summary['split']}`, {summary['requests']} requests, "
             f"{summary['wall_seconds']} s wall time (`python code/main.py`).\n")
    L.append("| Provider | Model | Purpose | Calls | Cache hits | Input tokens | Output tokens | Cost (USD) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for m in summary["models"]:
        L.append(f"| {m['provider']} | {m['model']} | {', '.join(m['purpose'])} | {m['calls']} | {m['cache_hits']} | "
                 f"{m['input_tokens']} | {m['output_tokens']} | {m['cost_usd']:.4f} |")
    t = summary["total"]
    L.append(f"| **Total** | | | **{t['calls']}** | | **{t['input_tokens']}** | **{t['output_tokens']}** | "
             f"**{t['cost_usd']:.4f}** |")
    L.append("")
    L.append(f"- Total tokens: {t['tokens']}; average tokens per request: {t['avg_tokens_per_request']}")
    L.append(f"- Estimated total cost: ${t['cost_usd']:.4f}; per request: ${t['avg_cost_per_request_usd']:.6f}")
    engines = Counter(v.split("(")[0] for v in summary.get("media_engines", {}).values())
    n_media = len(summary.get("media_engines", {}))
    L.append(f"- Media extraction engines used: {dict(engines)} ({n_media} PDF/PNG evidence files; OCR results "
             "are cached by file hash, so repeated runs re-use them)")
    L.append("")
    llm = [m for m in summary["models"] if m["provider"] == "Anthropic"]
    if not llm:
        L.append("No remote model was called in this run: `ANTHROPIC_API_KEY` was not set, so intent "
                 "classification used the deterministic multilingual rule classifier and screenshots used local "
                 "RapidOCR (PP-OCR models on ONNX Runtime, CPU). The evidence reasoning (identity resolution, "
                 "state reconstruction, policy, planning) is deterministic in every mode.\n")
    L.append("## Projected cost with Claude enabled (`--llm`)\n")
    n_png = sum(1 for v in summary.get("media_engines", {}).values() if not v.startswith("pdfplumber"))
    L.append("Estimated, not measured (no API key was available while building): intent prompt ~330 input / "
             "~90 output tokens per request; one screenshot (resized to <=1568 px, ~2,200 image tokens) ~2,300 "
             "input / ~700 output tokens. Prices: claude-opus-5-5 $4 / $20 per MTok.\n")
    intent_in, intent_out = 518 * 330, 518 * 90
    vis_in, vis_out = n_png * 2300, n_png * 700
    cost = (intent_in + vis_in) / 1e6 * 4 + (intent_out + vis_out) / 1e6 * 20
    L.append("| Model | Purpose | Calls | Input tokens | Output tokens | Est. cost (USD) |\n|---|---|---|---|---|---|")
    L.append(f"| claude-opus-5-5 | intent classification | 518 | {intent_in} | {intent_out} | "
             f"{intent_in / 1e6 * 4 + intent_out / 1e6 * 20:.4f} |")
    L.append(f"| claude-opus-5-5 | screenshot transcription | {n_png} | {vis_in} | {vis_out} | "
             f"{vis_in / 1e6 * 4 + vis_out / 1e6 * 20:.4f} |")
    L.append(f"| **Total** | | **{518 + n_png}** | **{intent_in + vis_in}** | **{intent_out + vis_out}** | **{cost:.4f}** |")
    L.append(f"\nAverage {(intent_in + vis_in + intent_out + vis_out) / 518:.0f} tokens and ${cost / 518:.5f} per "
             "request. Answers are cached, so re-runs cost nothing.")
    return "\n".join(L) + "\n"


def main():
    agent = DeletionAuditAgent(DATASET_DIR, log=lambda *a: None)
    sample = load_requests(DATASET_DIR / "sample_deletion_requests.csv")
    evals = load_requests(DATASET_DIR / "deletion_requests.csv")
    context = sample + evals
    full, _ = run_sample(agent, sample, context)
    ablations = [("Full agent", full)]
    for feat in linker.FEATURES:
        saved = dict(linker.FEATURES)
        linker.FEATURES[feat] = False
        m, _ = run_sample(agent, sample, context)
        ablations.append((f"without `{feat}`", m))
        linker.FEATURES.clear()
        linker.FEATURES.update(saved)
    custom = run_custom_cases(DATASET_DIR / "deletion_rules.csv")
    profile = eval_profile()
    (HERE / "evaluation_report.md").write_text(render_report(full, ablations, custom, profile), encoding="utf-8")
    usage_path = RUNS_DIR / "eval_usage.json"
    if usage_path.exists():
        (HERE / "usage_report.md").write_text(render_usage(json.loads(usage_path.read_text(encoding="utf-8"))),
                                              encoding="utf-8")
    print(json.dumps({k: v for k, v in full.items() if k != "per_case"}, indent=1))
    print("custom:", [(c["case_id"], c["steps_ok"]) for c in custom])
    print("ablations:", [(n, m["step_precision_recall_f1"][2]) for n, m in ablations])


if __name__ == "__main__":
    main()
