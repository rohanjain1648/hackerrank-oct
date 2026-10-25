# Ghost Data Still Exists - deletion-audit agent

HackerRank Orchestrate (October 2026). The full challenge contract is in
[`problem_statement.md`](./problem_statement.md); challenge and submission page:
https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

For every request in `dataset/deletion_requests.csv` the agent decides whether
deletion was actually requested, rebuilds the subject's current and historical
identifiers, finds every copy of their data across databases, replicas, logs,
exports, screenshots and PDFs, reconstructs each record's state **as of
`audit_at`**, applies `deletion_rules.csv`, and writes the ordered
FINDING -> REMEDIATION -> VERIFICATION plan to `output.csv`.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r code/requirements.txt

python code/main.py                    # 518 requests -> ./output.csv (validated)
python code/main.py --split sample     # 32 public samples, scored against sample_output.csv
python code/evaluation/main.py         # metrics, ablations, custom cases -> code/evaluation/*.md
```

Optional Claude layer (intent classification + screenshot transcription):

```bash
export ANTHROPIC_API_KEY=...           # PowerShell: $env:ANTHROPIC_API_KEY="..."
python code/main.py --llm              # model: claude-opus-5-5 (override with --model)
```

Without a key the run is fully offline and deterministic (same output on every
run). A clean run takes about one minute, most of it local OCR of the 11
screenshots on the first run; OCR and model answers are cached in `.cache/`.

## How it works

```
requests ──► intent ──► identity ──► evidence graph ──► linking ──► state @ audit_at ──► policy ──► plan
             (rules /    (current +   (94 files, 6 log    (direct ids,   (eligible files     (rules CSV)  (ordered,
              Claude)     audit log)   syntaxes, SQL,      later hints,   only, latest        validated)
                                       JSON, PDF, OCR)     workflow       copy wins, any
                                                           assignment)    replica counts)
```

| Stage | Module | What it does |
|---|---|---|
| Intent | `audit_agent/intent.py`, `llm.py` | Multilingual, negation-aware rules ("don't delete", "marketing lists only", follow-ups like "confirm my profile is gone"); optional Claude classifier with JSON-schema output. |
| Identity | `identity.py` | Current snapshot + `user_audit_log.csv` history: every email, phone and SHA-256 email hash the person ever had. Names are never used as keys (three names are shared by different people). |
| Evidence | `evidence/` | One parser per format: service-export JSON, snapshot bundles, record ledgers, SQLite snapshots (executed in memory, every replica table kept separate), hand-over text, six log syntaxes joined by trace id, PDFs (pdfplumber), screenshots (RapidOCR or Claude vision). Output: ~2,900 normalised observations. |
| Linking | `linker.py` | **Direct**: an identifier or lookup key of the subject, observed inside the request life-cycle. **Hint**: later evidence may identify a resource touched by the request's workflow, never decide its state. **Workflow assignment**: 404-only profiles and anonymised analytics carry no identifier, so deletion-worker activities are assigned globally across all requests using a hard timing window, workflow-claim consistency (submission result mirrors the reported status), evidence affinity and the policy-action cadence, then elimination. |
| State | `reasoner.py` | Only files captured at or before `audit_at`; the latest observation wins; any surviving copy (primary or replica) makes a record present; a snapshot that enumerates profiles for the subject proves absence by omission. |
| Policy | `policy.py` | Data-driven from `deletion_rules.csv` (profiles/search/exports deleted, support messages redacted, attachments deleted, analytics anonymised, security logs and legal holds retained, backups retained with restore blocked, tombstones reduced to the stable `user_id`, data flows disabled). |
| Plan | `reasoner.py`, `main.py` | Findings cite the evidence file that proves them; remediation disables flows and blocks restores before changing records; verification covers every record of the case (with evidence for COMPLETE cases). Contract checks run on every output. |

Each run also writes a per-case reasoning trace (identifiers used, every
resource's link type, category, rule, state, cited evidence, assignment
candidates and scores) to `.cache/runs/<split>_trace.json`.

## Results

On the 32 labelled samples: status accuracy 1.000, step precision/recall/F1
1.000 / 0.985 / 0.992, sequence accuracy 0.906. The three missed steps
reference resources that appear in no participant-visible file. Six extra
hand-written cases (historical email + replica drift, look-alike decoy,
evidence captured after `audit_at`, exposed tombstone, negated non-deletion
request, identifier-less profile) all pass. Details, ablations and the failure
log: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).
Token and cost accounting: [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## Design decisions

- **Deterministic core, narrow AI.** Evidence reasoning must be auditable and
  reproducible, so identity resolution, state reconstruction and policy are
  code. AI is used where it helps: reading screenshots (local neural OCR, or
  Claude vision) and understanding free-text intent (rules, or Claude).
- **Claims are not proof.** Workflow statuses, `reported_result` values and
  data-catalogue links are treated as claims. A catalogue link is rejected
  when the record on screen shows identifiers of a different known person.
- **Time discipline.** Nothing captured after `audit_at` changes state or is
  cited; old evidence from an earlier deletion of the same person is ignored.
- **No hardcoded answers.** No case ids, labels or file-specific outputs are
  embedded; the parsers are per format, and the 32 sample answers are used
  only for evaluation.

## Repository layout

```
code/
  main.py                 CLI entry point (+ output validation)
  requirements.txt
  audit_agent/            the agent (intent, identity, evidence/, linker, reasoner, policy, llm, usage)
  evaluation/
    main.py               evaluation workflow
    metrics.py            status / step / field / sequence metrics
    custom_cases.py       participant-authored evaluation cases
    evaluation_report.md
    usage_report.md
output.csv                predictions for dataset/deletion_requests.csv
log.txt                   AI-assisted development transcript (AGENTS.md format)
```
