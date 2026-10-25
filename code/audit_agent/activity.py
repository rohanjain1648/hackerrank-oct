"""Global activity stream (system_activity.csv).

Activity rows are *claims*: a ``SUCCESS`` deletion does not prove absence.
They are still valuable for two things: tying resources that carry no
identifiers (anonymised analytics, 404 profiles) to the deletion workflow that
touched them, and spotting processes (syncs, restores, connectors) that can
recreate data.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .models import Activity
from .textutil import RESOURCE_RE, parse_time


class ActivityLog:
    def __init__(self, path: Path):
        self.rows: list[Activity] = []
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                details = row["details"]
                mentioned = [m for m in RESOURCE_RE.findall(details) if m != row["resource_id"]]
                self.rows.append(
                    Activity(
                        activity_id=row["activity_id"],
                        event_time=parse_time(row["event_time"]),
                        event_type=row["event_type"],
                        source_system=row["source_system"],
                        target_system=row["target_system"],
                        resource_id=row["resource_id"],
                        reported_result=row["reported_result"],
                        details=details,
                        mentioned=mentioned,
                    )
                )
        self.rows.sort(key=lambda a: (a.event_time, a.activity_id))
        self.by_resource: dict[str, list[Activity]] = defaultdict(list)
        for act in self.rows:
            self.by_resource[act.resource_id].append(act)
            for res in act.mentioned:
                self.by_resource[res].append(act)

    def for_resource(self, resource_id: str) -> list[Activity]:
        return self.by_resource.get(resource_id, [])

    def between(self, start, end, event_types: set[str] | None = None) -> list[Activity]:
        return [
            a
            for a in self.rows
            if start <= a.event_time <= end and (event_types is None or a.event_type in event_types)
        ]
