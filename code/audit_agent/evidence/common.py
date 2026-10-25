"""Helpers for building Observations from parser output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Observation, default_system, resource_kind


@dataclass
class FileMeta:
    evidence_id: str
    path: str
    origin: str
    captured_at: datetime
    # Systems this file enumerates completely for the subjects it contains
    # (snapshot-style sources). Lets the reasoner prove absence by omission.
    coverage: dict[str, datetime] = field(default_factory=dict)
    # Subset of coverage that is stated by the file itself (a profile table or
    # an explicit snapshot scope) rather than implied by a customer-data dump.
    explicit_coverage: set[str] = field(default_factory=set)
    kind: str = ""
    text: str = ""  # extracted text for documents/images (OCR or PDF)


def make_obs(meta: FileMeta, observed_at: datetime | None, resource_id: str, state: dict,
             source: str, location: str | None = None, raw: dict | None = None,
             system: str | None = None) -> Observation | None:
    if not resource_id or resource_kind(resource_id) is None:
        return None
    return Observation(
        evidence_id=meta.evidence_id,
        captured_at=meta.captured_at,
        observed_at=observed_at or meta.captured_at,
        system=system or default_system(resource_id),
        resource_id=resource_id,
        source=source,
        presence=state.get("presence"),
        exposure=state.get("exposure"),
        flow=state.get("flow"),
        restore=state.get("restore"),
        category=state.get("category"),
        keys=set(state.get("keys") or ()),
        exposed=set(state.get("exposed") or ()),
        location=location,
        note=state.get("note", ""),
        raw=raw or {},
    )
