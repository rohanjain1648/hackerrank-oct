"""Resource-to-subject linking.

Three kinds of link, from strongest to weakest:

1. **Direct** - an observation carries one of the subject's current or
   historical identifiers (user_id, email, phone, email hash) or was looked up
   with one, and was observed inside the request's life-cycle
   (``requested_at`` .. ``audit_at``). Old observations from an earlier
   deletion episode of the same person are deliberately ignored.
2. **Identity hint** - a resource touched by the case's deletion workflow
   that some *later* observation ties to the subject (e.g. a snapshot captured
   after ``audit_at``). Later evidence may identify a resource; it never
   decides its state.
3. **Workflow assignment** - records that no longer carry any identifier
   (404 profiles, anonymised analytics) can only be tied to a case through
   the deletion-worker activity that processed them. Several cases share a
   workflow window, so these activities are assigned globally: forced
   choices first (elimination), then the best-scoring candidate. Scores use
   timing, the evidence file the record is rendered in, and the fixed cadence
   of a workflow's policy actions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .models import Activity, Observation, Person, Request, resource_kind

WORKFLOW_EVENTS = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW",
                   "BLOCK_RESTORE", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD"}
WINDOW_AFTER_REPORT = timedelta(hours=4)
HINT_HORIZON = timedelta(days=45)
# Cadence of a workflow's successful policy actions: analytics, support,
# data flow, backup, each ~7 minutes apart with alternating 7:29 / 6:30 gaps.
CADENCE_INDEX = {("ANONYMIZE_RECORD", "analytics"): 0, ("REDACT_RECORD", "support"): 1,
                 ("DISABLE_DATA_FLOW", "data_flow"): 2, ("BLOCK_RESTORE", "backup"): 3}
GAPS = (390.0, 449.0)
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

# Linking signals that can be switched off for ablation studies (evaluation/main.py).
FEATURES = {
    "later_identity_hints": True,   # later evidence may identify (never decide) a workflow resource
    "workflow_timing": True,        # hard timing window relative to reported_at
    "status_result": True,          # profile-submission result mirrors the reported status
    "cadence": True,                # fixed cadence of a workflow's policy actions
    "evidence_affinity": True,      # record rendered in the case's evidence file
}


@dataclass
class CaseLinks:
    request: Request
    person: Person | None
    is_deletion: bool
    direct: dict[str, list[Observation]] = field(default_factory=dict)
    hinted: dict[str, list[Observation]] = field(default_factory=dict)
    assigned: dict[str, Activity] = field(default_factory=dict)
    activities: dict[str, list[Activity]] = field(default_factory=lambda: defaultdict(list))
    notes: list[str] = field(default_factory=list)
    provisional_complete: bool = True
    account_coverage: bool = False  # a file with the subject's records enumerates profiles

    @property
    def case_id(self) -> str:
        return self.request.case_id

    def resources(self) -> set[str]:
        return set(self.direct) | set(self.hinted) | set(self.assigned)

    def has_kind(self, kind: str) -> bool:
        return any(resource_kind(r) == kind for r in self.resources())


def _case_no(case_id: str) -> int:
    digits = "".join(ch for ch in case_id if ch.isdigit())
    return int(digits) if digits else 0


class Linker:
    def __init__(self, store, activity, identities, violation_fn):
        self.store = store
        self.activity = activity
        self.identities = identities
        self.violation_fn = violation_fn  # (resource_id, observations, audit_at) -> bool

    # -- direct ----------------------------------------------------------
    def direct_links(self, case: CaseLinks) -> None:
        p, req = case.person, case.request
        for rid in sorted(self.store.resources_for_keys(p.keys)):
            obs = [o for o in self.store.by_resource[rid]
                   if (o.keys & p.keys) and req.requested_at <= o.observed_at <= req.audit_at]
            if obs:
                case.direct[rid] = obs

    def _in_window(self, case: CaseLinks, when) -> bool:
        r = case.request
        return r.requested_at - timedelta(minutes=1) <= when <= r.reported_at + WINDOW_AFTER_REPORT

    def _all_owners(self, rid: str) -> set[str]:
        """Everyone any observation of ``rid`` identifies; such a record is not
        identifier-less and may only be assigned to one of these people."""
        owners = set()
        for o in self.store.by_resource.get(rid, []):
            for k in o.keys:
                uid = self.identities.owner_of(k)
                if uid:
                    owners.add(uid)
        return owners

    def _hint_owners(self, rid: str, when) -> set[str]:
        owners = set()
        for o in self.store.by_resource.get(rid, []):
            if when - timedelta(days=1) <= o.observed_at <= when + HINT_HORIZON:
                for k in o.keys:
                    uid = self.identities.owner_of(k)
                    if uid:
                        owners.add(uid)
        return owners

    # -- global assignment -----------------------------------------------
    def link_all(self, cases: list[CaseLinks]) -> None:
        deletion_cases = [c for c in cases if c.is_deletion and c.person]
        for c in deletion_cases:
            self.direct_links(c)
        by_user = defaultdict(list)
        for c in deletion_cases:
            by_user[c.person.user_id].append(c)

        pools: dict[str, list[tuple[Activity, list[CaseLinks]]]] = defaultdict(list)
        for act in self.activity.rows:
            if act.source_system != "deletion_worker" or act.event_type not in WORKFLOW_EVENTS:
                continue
            rid = act.resource_id
            cands = [c for c in deletion_cases if self._in_window(c, act.event_time)]
            if not cands:
                continue
            owners = [c for c in cands if rid in c.direct]
            if owners:
                for c in owners:
                    c.activities[rid].append(act)
                continue
            hint_users = self._hint_owners(rid, act.event_time) if FEATURES["later_identity_hints"] else set()
            hinted = [c for c in cands if c.person.user_id in hint_users]
            if hinted:
                for c in hinted:
                    c.hinted[rid] = [o for o in self.store.by_resource.get(rid, [])]
                    c.activities[rid].append(act)
                    c.notes.append(f"{rid}: tied by a later observation to {c.person.user_id} ({act.activity_id})")
                continue
            kind = resource_kind(rid)
            known_owners = self._all_owners(rid)
            fitting = [c for c in cands if (not FEATURES["workflow_timing"] or self._cadence_fits(kind, act, c))
                       and (not known_owners or c.person.user_id in known_owners)]
            if kind == "account" and act.event_type == "DELETE_RECORD" and fitting:
                pools["account"].append((act, fitting))
            elif kind == "analytics" and act.event_type == "ANONYMIZE_RECORD" and fitting:
                pools["analytics"].append((act, fitting))

        for c in deletion_cases:
            self._keep_single_account(c)
            files = {o.evidence_id for obs in c.direct.values() for o in obs}
            c.account_coverage = any("account_service" in self.store.files[e].coverage for e in files)
            c.provisional_complete = not any(
                self.violation_fn(rid, obs, c.request.audit_at)
                for rid, obs in list(c.direct.items()) + list(c.hinted.items()))
        self._assign(pools["account"], "account")
        self._assign(pools["analytics"], "analytics")

    @staticmethod
    def _cadence_fits(kind: str, act: Activity, c: CaseLinks) -> bool:
        """Hard timing constraint of the deletion workflow relative to reported_at.

        Single-request workflows log "Target resource: ..." moments before the
        status is reported; batch workflows submit profiles 25-95 minutes and
        run anonymisation ~30 minutes before it, or report failures afterwards."""
        off = (act.event_time - c.request.reported_at).total_seconds() / 60.0
        if "Target resource" in act.details:
            return abs(off) <= 15
        if kind == "account":
            return -95 <= off <= -25
        return -40 <= off <= -15 or (act.reported_result != "SUCCESS" and 0 <= off <= 240)

    def _keep_single_account(self, c: CaseLinks) -> None:
        accounts = [r for r in c.resources() if resource_kind(r) == "account"]
        if len(accounts) <= 1:
            return

        def rank(rid):
            in_wf = any(self._in_window(c, a.event_time) for a in self.activity.for_resource(rid)
                        if a.event_type == "DELETE_RECORD")
            obs = c.direct.get(rid) or c.hinted.get(rid) or []
            latest = max((o.observed_at for o in obs), default=c.request.requested_at)
            return (in_wf, latest)

        keep = max(accounts, key=rank)
        for rid in accounts:
            if rid != keep:
                c.direct.pop(rid, None)
                c.hinted.pop(rid, None)
                c.notes.append(f"{rid}: dropped, {keep} is the profile processed by this request")

    def _observed_for(self, rid: str, c: CaseLinks, since) -> list[Observation]:
        return [o for o in self.store.by_resource.get(rid, [])
                if o.captured_at <= c.request.audit_at and o.observed_at >= since]

    def _score(self, kind: str, act: Activity, c: CaseLinks) -> float:
        rep = c.request.reported_at
        off = (act.event_time - rep).total_seconds() / 60.0
        score = 0.0
        observed = self._observed_for(act.resource_id, c, act.event_time)
        case_files = {o.evidence_id for obs in c.direct.values() for o in obs if o.captured_at <= c.request.audit_at}
        if observed and FEATURES["evidence_affinity"]:
            score += 1.0
            if any(o.evidence_id in case_files for o in observed):
                score += 1.0
        if kind == "account":
            # The workflow logs its own claim: the profile submission result
            # mirrors the status it later reported for this request.
            if FEATURES["status_result"] and STATUS_RESULT.get(c.request.reported_status) == act.reported_result:
                score += 3.0
            if "Target resource" in act.details:
                score += 3.0 if abs(off) <= 15 else 0.0
            elif -100 <= off <= -20:
                score += 1.0
            if c.provisional_complete and (observed or c.account_coverage):
                score += 1.0
        else:
            if -40 <= off <= -15:
                score += 1.0
            if FEATURES["cadence"]:
                score += self._cadence_score(act, c)
            if c.provisional_complete:
                score += 0.5
        return score

    def _cadence_score(self, act: Activity, c: CaseLinks) -> float:
        known = {0: act.event_time}
        for rid, acts in c.activities.items():
            for a in acts:
                idx = CADENCE_INDEX.get((a.event_type, resource_kind(rid)))
                if idx and a.reported_result == "SUCCESS" and abs((a.event_time - act.event_time).total_seconds()) < 1800:
                    known[idx] = a.event_time
        if len(known) < 2:
            return 0.0
        idxs = sorted(known)
        gaps = []
        for i, j in zip(idxs, idxs[1:]):
            g = (known[j] - known[i]).total_seconds()
            if j - i == 1:
                match = [k for k, ref in enumerate(GAPS) if abs(g - ref) <= 3]
                if not match:
                    return -1.0
                gaps.append((i, match[0]))
            elif abs(g - (j - i) * sum(GAPS) / 2) > 35:
                return -1.0
        for (i1, m1), (i2, m2) in zip(gaps, gaps[1:]):
            if i2 == i1 + 1 and m1 == m2:
                return -1.0  # gaps must alternate
        return 2.0 if gaps and gaps[0][0] == 0 else 0.5

    def _assign(self, pool, kind: str) -> None:
        items = []
        for act, cands in pool:
            items.append({"act": act, "cands": cands,
                          "scores": {c.case_id: self._score(kind, act, c) for c in cands}})

        def feasible(item):
            return [c for c in item["cands"] if not c.has_kind(kind)]

        while True:
            items = [it for it in items if feasible(it)]
            if not items:
                break
            forced = next((it for it in items if len(feasible(it)) == 1), None)
            if forced is not None:
                self._give(feasible(forced)[0], forced, kind, "eliminated alternatives")
                items.remove(forced)
                continue
            self._best(items, feasible, kind)

    def _best(self, items, feasible, kind):
        def margin(it):
            s = sorted((it["scores"][c.case_id] for c in feasible(it)), reverse=True)
            return (s[0] - (s[1] if len(s) > 1 else -9), s[0], -it["act"].event_time.timestamp())

        # ties: earliest activity goes to the lowest case number

        it = max(items, key=margin)
        cands = sorted(feasible(it), key=lambda c: (-it["scores"][c.case_id], _case_no(c.case_id)))
        self._give(cands[0], it, kind, f"best workflow fit (score {it['scores'][cands[0].case_id]:.1f})")
        items.remove(it)

    @staticmethod
    def _give(case: CaseLinks, item, kind: str, why: str) -> None:
        act = item["act"]
        case.assigned[act.resource_id] = act
        case.activities[act.resource_id].append(act)
        rivals = ", ".join(f"{cid}={sc:.1f}" for cid, sc in sorted(item["scores"].items()))
        case.notes.append(f"{act.resource_id}: assigned from {act.activity_id} ({why}; candidates {rivals})")
