"""State reconstruction at ``audit_at`` and policy decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .models import Observation, Step, default_category, default_system, resource_kind
from .policy import OPERATION_STATE, TREATMENTS, Policy, step_sort_key


@dataclass
class ResourceState:
    resource_id: str
    presence: str | None = None
    exposure: str | None = None
    flow: str | None = None
    restore: str | None = None
    evidence_id: str = ""
    observed_at: object = None
    basis: str = ""  # explicit | coverage | assumed


def eligible(observations, audit_at) -> list[Observation]:
    """Evidence usable for a case: captured (and observed) no later than audit_at."""
    return [o for o in observations if o.captured_at <= audit_at and o.observed_at <= audit_at]


def state_at(resource_id: str, observations, audit_at) -> ResourceState | None:
    obs = eligible(observations, audit_at)
    if not obs:
        return None
    latest = max(obs, key=lambda o: (o.observed_at, o.captured_at))
    # All copies the latest file reports around that moment (primary + replicas).
    group = [o for o in obs if o.evidence_id == latest.evidence_id
             and o.observed_at >= latest.observed_at - timedelta(hours=1)]
    st = ResourceState(resource_id, evidence_id=latest.evidence_id, observed_at=latest.observed_at, basis="explicit")
    pres = {o.presence for o in group}
    st.presence = "PRESENT" if "PRESENT" in pres else ("ABSENT" if "ABSENT" in pres else None)
    if any(o.exposure == "IDENTIFIABLE" or (o.exposure is None and o.exposed) for o in group if o.presence != "ABSENT"):
        st.exposure = "IDENTIFIABLE"
    elif any(o.exposure == "CLEAN" for o in group):
        st.exposure = "CLEAN"
    flows = {o.flow for o in group if o.flow}
    st.flow = "ACTIVE" if "ACTIVE" in flows else ("DISABLED" if flows else None)
    restores = {o.restore for o in group if o.restore}
    st.restore = "ACTIVE" if "ACTIVE" in restores else ("BLOCKED" if restores else None)
    return st


def categorize(resource_id: str, observations, activities) -> str:
    kind = resource_kind(resource_id)
    explicit = {o.category for o in observations if o.category}
    worker = {a.event_type for a in activities if a.source_system == "deletion_worker"}
    if kind == "analytics":
        if "SECONDARY_IDENTIFIER" in explicit or "DELETE_RECORD" in worker:
            return "SECONDARY_IDENTIFIER"
        if "PRODUCT_TELEMETRY" in explicit:
            return "PRODUCT_TELEMETRY"
        return "ANALYTICS_PROFILE"
    if kind in ("support", "object"):
        if "SUPPORT_ATTACHMENT" in explicit:
            return "SUPPORT_ATTACHMENT"
        if "SUPPORT_MESSAGE" in explicit:
            return "SUPPORT_MESSAGE"
        if "DELETE_RECORD" in worker:
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_ATTACHMENT" if kind == "object" else "SUPPORT_MESSAGE"
    for cat in ("USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "MARKETING_PROFILE", "CACHE_ENTRY",
                "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
                "ACCOUNT_TOMBSTONE", "DATA_FLOW"):
        if cat in explicit and default_category(resource_id) == cat:
            return cat
    return default_category(resource_id) or "USER_PROFILE"


def is_violation(treatment: str, st: ResourceState | None) -> bool:
    if st is None:
        return False
    if treatment == "DELETE":
        return st.presence == "PRESENT"
    if treatment in ("REDACT", "ANONYMIZE"):
        return st.presence != "ABSENT" and st.exposure == "IDENTIFIABLE"
    if treatment == "DISABLE_FLOW":
        return st.flow == "ACTIVE"
    if treatment == "BLOCK_RESTORE":
        return st.restore == "ACTIVE"
    return False


@dataclass
class Decision:
    resource_id: str
    system_id: str
    category: str
    treatment: str
    rule_id: str | None
    state: ResourceState | None
    violation: bool
    link: str
    notes: list[str] = field(default_factory=list)


class Reasoner:
    def __init__(self, policy: Policy, store):
        self.policy = policy
        self.store = store

    def violation_for(self, resource_id, observations, audit_at) -> bool:
        cat = categorize(resource_id, observations, [])
        treatment, _ = self.policy.treatment(cat)
        return is_violation(treatment, state_at(resource_id, observations, audit_at))

    def coverage_absence(self, case, audit_at, since) -> ResourceState | None:
        """Prove a profile's absence from a snapshot that enumerates account_service
        for this subject (the subject's other records are in the same file)."""
        case_files = {o.evidence_id for obs in case.direct.values() for o in obs}
        best = None
        for eid, meta in self.store.files.items():
            t = meta.coverage.get("account_service")
            if t is None or meta.captured_at > audit_at or t < since or eid not in case_files:
                continue
            if best is None or t > best[1]:
                best = (eid, t)
        if best:
            return ResourceState("", presence="ABSENT", evidence_id=best[0], observed_at=best[1], basis="coverage")
        return None

    def coverage_fallback(self, case, audit_at) -> ResourceState | None:
        """No file holding the subject's records enumerates profiles: use the first
        account-table snapshot taken after the workflow reported its status."""
        best = None
        for eid, meta in self.store.files.items():
            t = meta.coverage.get("account_service")
            if (t is None or "account_service" not in meta.explicit_coverage
                    or meta.captured_at > audit_at or meta.captured_at < case.request.reported_at):
                continue
            # an account-dedicated snapshot beats a general customer-data dump
            rank = (meta.explicit_coverage != {"account_service"}, meta.captured_at)
            if best is None or rank < best[1]:
                best = (eid, rank)
        if best:
            return ResourceState("", presence="ABSENT", evidence_id=best[0], observed_at=best[1][1], basis="coverage")
        return None

    def primary_file(self, case, audit_at) -> str:
        cands = [(o.captured_at, o.evidence_id) for obs in case.direct.values() for o in obs if o.captured_at <= audit_at]
        return max(cands)[1] if cands else ""

    def decide(self, case) -> list[Decision]:
        req = case.request
        decisions = []
        for rid in sorted(case.resources()):
            observations = self.store.by_resource.get(rid, [])
            acts = case.activities.get(rid, [])
            cat = categorize(rid, observations, acts)
            treatment, rule = self.policy.treatment(cat)
            st = state_at(rid, observations, req.audit_at)
            link = "direct" if rid in case.direct else ("hint" if rid in case.hinted else "workflow")
            notes = []
            if resource_kind(rid) == "account":
                cov = self.coverage_absence(case, req.audit_at, req.requested_at)
                if cov is None and st is None:
                    cov = self.coverage_fallback(case, req.audit_at)
                if cov and (st is None or (st.observed_at and cov.observed_at > st.observed_at)):
                    cov.resource_id = rid
                    st = cov
                    notes.append(f"absence proven by {cov.evidence_id}, which enumerates account_service")
            if st is None:
                # Identified but never observed before audit_at: assume the
                # workflow's target state, cite the case's main evidence file.
                st = ResourceState(rid, basis="assumed", evidence_id=self.primary_file(case, req.audit_at))
                notes.append("no eligible observation; target state assumed")
            decisions.append(Decision(rid, default_system(rid), cat, treatment, rule.rule_id if rule else None,
                                      st, is_violation(treatment, st), link, notes))
        return decisions

    @staticmethod
    def plan(decisions: list[Decision]) -> tuple[str, list[Step]]:
        violations = [d for d in decisions if d.violation]
        status = "INCOMPLETE" if violations else "COMPLETE"
        steps: list[Step] = []
        for d in decisions:
            finding, remediation, verification = TREATMENTS[d.treatment]
            if d.violation:
                steps.append(Step("FINDING", d.system_id, d.resource_id, finding, OPERATION_STATE[finding],
                                  d.state.evidence_id))
                steps.append(Step("REMEDIATION", d.system_id, d.resource_id, remediation,
                                  OPERATION_STATE[remediation], ""))
            evidence = "" if status == "INCOMPLETE" else (d.state.evidence_id if d.state else "")
            steps.append(Step("VERIFICATION", d.system_id, d.resource_id, verification,
                              OPERATION_STATE[verification], evidence))
        steps.sort(key=step_sort_key)
        return status, steps
