"""Deletion-audit agent: orchestrates the full pipeline for a batch of requests."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field

from .activity import ActivityLog
from .evidence import load_evidence
from .identity import IdentityIndex
from .intent import IntentResult, RuleClassifier
from .linker import CaseLinks, Linker
from .models import Request
from .paths import DATASET_DIR
from .policy import Policy
from .reasoner import Reasoner
from .textutil import parse_time, subject_keys


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    intent: IntentResult
    user_id: str | None
    trace: dict = field(default_factory=dict)

    def steps_json(self) -> str:
        return json.dumps([s.as_dict(i + 1) for i, s in enumerate(self.steps)], ensure_ascii=False,
                          separators=(",", ":"))


def case_order(case_id: str):
    m = re.search(r"(\d+)$", case_id)
    return (int(m.group(1)) if m else 0, case_id)


def load_requests(path) -> list[Request]:
    with open(path, encoding="utf-8", newline="") as fh:
        return [Request(r["case_id"].strip(), r["email"].strip(), parse_time(r["requested_at"]),
                        r["reported_status"].strip(), parse_time(r["reported_at"]), parse_time(r["audit_at"]),
                        r["request_text"]) for r in csv.DictReader(fh)]


class DeletionAuditAgent:
    def __init__(self, dataset_dir=DATASET_DIR, llm=None, usage=None, log=print):
        self.dataset_dir = dataset_dir
        self.llm = llm
        self.usage = usage
        self.log = log
        self.identities = IdentityIndex(dataset_dir / "user_identities.csv", dataset_dir / "user_audit_log.csv")
        self.activity = ActivityLog(dataset_dir / "system_activity.csv")
        self.policy = Policy(dataset_dir / "deletion_rules.csv")
        vision = llm.vision_rows if (llm is not None and llm.vision_enabled) else None
        self.store = load_evidence(dataset_dir, self.activity, usage=usage, vision=vision, log=log)
        self._augment_from_catalog()
        self.reasoner = Reasoner(self.policy, self.store)
        self.rules = RuleClassifier()

    # Data-catalogue activities name the owner of a record shown on a screen or
    # in a document. They are claims: accepted only when the record itself does
    # not show identifiers belonging to someone else.
    def _augment_from_catalog(self) -> None:
        for entry in self.store.catalog:
            claim = subject_keys(entry["email"]) | {entry["user_id"]}
            person = self.identities.people.get(entry["user_id"])
            for obs in self.store.by_resource.get(entry["resource_id"], []):
                if not obs.source.startswith("media:"):
                    continue
                shown = {k for k in obs.keys | obs.exposed if "@" in k or k.startswith("user_")}
                # Reject the claim only when the screen names a *different known* person;
                # identifiers that resolve to nobody cannot contradict it.
                foreign = {k for k in shown if self.identities.owner_of(k) not in (None, entry["user_id"])}
                if foreign:
                    obs.note = (obs.note + f" catalogue claim {entry['activity_id']} rejected: record shows "
                                f"{sorted(foreign)}").strip()
                    continue
                obs.keys |= claim
                for k in claim:
                    self.store.by_key[k].add(entry["resource_id"])
                meta = self.store.files[obs.evidence_id]
                if person is not None:
                    # The screen/document is about this record: any direct
                    # identifier of the subject printed on it is exposed.
                    text = meta.text.lower()
                    seen = {e for e in person.emails if e in text}
                    if person.name and person.name.lower() in text:
                        seen.add("name:" + person.name.lower())
                    if seen and "[redacted]" not in text:
                        obs.exposed |= seen
                        obs.exposure = "IDENTIFIABLE"
                # Other rows of the same kind on that screen are page context
                # unless they carry identifiers of their own.
                kind = obs.resource_id.rsplit("_", 1)[0]
                for other in self.store.observations:
                    if (other.evidence_id == obs.evidence_id and other.resource_id != obs.resource_id
                            and other.resource_id.rsplit("_", 1)[0] == kind
                            and not {k for k in other.keys if "@" in k}):
                        for k in other.keys:
                            self.store.by_key.get(k, set()).discard(other.resource_id)
                        other.keys = set()
                        other.note = (other.note + " context row; catalogue names "
                                      f"{obs.resource_id} as the subject's record").strip()

    def classify(self, req: Request) -> IntentResult:
        rule = self.rules.classify(req.request_text)
        if self.llm is None or not self.llm.enabled:
            return rule
        llm = self.llm.classify_intent(req.case_id, req.request_text)
        if llm is None:
            return rule
        if llm.is_deletion != rule.is_deletion:
            llm.rationale += f" | rules disagreed ({rule.label})"
            if llm.confidence < 0.7:
                return rule
        return llm

    def run(self, requests: list[Request], context_requests: list[Request] | None = None) -> list[CaseResult]:
        """Audit ``requests``; ``context_requests`` (all known requests) are used
        only so that shared workflow windows can be disambiguated globally."""
        everything = {r.case_id: r for r in (context_requests or [])}
        everything.update({r.case_id: r for r in requests})
        cases: dict[str, CaseLinks] = {}
        intents: dict[str, IntentResult] = {}
        for req in everything.values():
            intent = self.classify(req)
            intents[req.case_id] = intent
            cases[req.case_id] = CaseLinks(req, self.identities.resolve_email(req.email), intent.is_deletion)
        Linker(self.store, self.activity, self.identities, self.reasoner.violation_for).link_all(list(cases.values()))

        results = []
        for req in sorted(requests, key=lambda r: case_order(r.case_id)):
            case, intent = cases[req.case_id], intents[req.case_id]
            if not intent.is_deletion:
                results.append(CaseResult(req.case_id, "NOT_APPLICABLE", [], intent,
                                          case.person.user_id if case.person else None,
                                          {"intent": intent.__dict__}))
                continue
            if case.person is None:
                results.append(CaseResult(req.case_id, "INCOMPLETE", [], intent, None,
                                          {"error": "subject could not be resolved"}))
                continue
            decisions = self.reasoner.decide(case)
            status, steps = self.reasoner.plan(decisions)
            trace = {
                "intent": intent.__dict__,
                "subject": case.person.user_id,
                "identifiers": sorted(k for k in case.person.keys if not k.startswith("tel:")),
                "decisions": [{
                    "resource": d.resource_id, "category": d.category, "treatment": d.treatment,
                    "rule": d.rule_id, "link": d.link, "violation": d.violation,
                    "state": {k: getattr(d.state, k) for k in ("presence", "exposure", "flow", "restore",
                                                               "evidence_id", "basis")},
                    "notes": d.notes,
                } for d in decisions],
                "link_notes": case.notes,
            }
            results.append(CaseResult(req.case_id, status, steps, intent, case.person.user_id, trace))
        return results


def write_output(results: list[CaseResult], path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in sorted(results, key=lambda r: case_order(r.case_id)):
            w.writerow([r.case_id, r.status, r.steps_json()])
