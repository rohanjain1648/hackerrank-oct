"""Core data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# resource-id prefix -> (system_id used in steps, default data category)
RESOURCE_KINDS: dict[str, tuple[str, str]] = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support": ("support_platform", "SUPPORT_MESSAGE"),
    "export": ("export_service", "USER_EXPORT"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "object": ("object_storage", "SUPPORT_ATTACHMENT"),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
}


def resource_kind(resource_id: str) -> str | None:
    best = None
    for prefix in RESOURCE_KINDS:
        if resource_id.startswith(prefix + "_") and (best is None or len(prefix) > len(best)):
            best = prefix
    return best


def default_system(resource_id: str) -> str | None:
    kind = resource_kind(resource_id)
    return RESOURCE_KINDS[kind][0] if kind else None


def default_category(resource_id: str) -> str | None:
    kind = resource_kind(resource_id)
    return RESOURCE_KINDS[kind][1] if kind else None


@dataclass
class Person:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime | None
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    history: list[dict] = field(default_factory=list)

    @property
    def keys(self) -> set[str]:
        return {self.user_id} | self.emails | self.hashes | {"tel:" + p for p in self.phones}


@dataclass
class Request:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass
class Observation:
    """One normalised statement an evidence file makes about one resource.

    ``keys`` are subject link keys (user refs, lower-cased emails, hashes,
    ``tel:`` phones) that tie the record to a person, including lookup keys
    used to query it. ``exposed`` lists the direct identifiers the record
    itself still contains, which is what policy cares about.
    """

    evidence_id: str
    captured_at: datetime  # manifest capture time of the evidence file
    observed_at: datetime  # time the record state was observed
    system: str
    resource_id: str
    source: str  # parser/format tag
    presence: str | None = None  # PRESENT | ABSENT
    exposure: str | None = None  # IDENTIFIABLE | CLEAN
    flow: str | None = None  # ACTIVE | DISABLED
    restore: str | None = None  # ACTIVE | BLOCKED
    category: str | None = None  # explicit data category when the source states it
    keys: set[str] = field(default_factory=set)
    exposed: set[str] = field(default_factory=set)
    location: str | None = None  # table / replica / snapshot collection
    note: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str
    mentioned: list[str] = field(default_factory=list)


@dataclass
class Step:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str = ""

    def as_dict(self, step_no: int) -> dict:
        return {
            "step_no": step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }
