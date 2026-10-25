"""SQLite-compatible snapshot exports.

Each file is executed in an in-memory SQLite database (it is plain SQL text).
Every table holding customer records becomes observations tagged with the
table name, so replication drift (a row that survives only in a replica) is
visible to the reasoner.
"""

from __future__ import annotations

import json
import re
import sqlite3

from ..models import default_system, resource_kind
from ..textutil import parse_time
from .common import FileMeta, make_obs
from .records import interpret

CUSTOMER_SYSTEMS = (
    "account_service", "search_service", "analytics_platform", "support_platform",
    "export_service", "integration_service", "backup_service", "security_service",
    "identity_service",
)


def parse_sql(meta: FileMeta, path) -> list:
    text = open(path, encoding="utf-8").read()
    header_time = None
    m = re.search(r"--\s*Captured at:\s*(\S+)", text)
    if m:
        header_time = parse_time(m.group(1))
    m = re.search(r"snapshot_completed_at=(\S+)", text)
    completed = parse_time(m.group(1)) if m else None
    snap_time = header_time or completed or meta.captured_at

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        scope: dict[str, object] = {}
        if "snapshot_scope" in tables:
            for row in con.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
                scope[row["origin_system"]] = parse_time(row["captured_at"])
        out = []
        seen_systems: set[str] = set()
        for table in tables:
            if table == "snapshot_scope":
                continue
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            for row in con.execute(f'SELECT * FROM "{table}"'):
                rec = dict(row)
                if "record_json" in cols:
                    body = json.loads(rec["record_json"])
                    rid = rec["resource_id"]
                    system = rec.get("origin_system") or default_system(rid)
                    location = table
                else:
                    body = rec
                    rid = rec.get("resource_id")
                    system = default_system(rid or "")
                    location = table
                    if "user_id" in body and "customer_reference" not in body:
                        body = {**body, "customer_reference": body["user_id"]}
                kind = resource_kind(rid or "")
                if kind is None:
                    continue
                t = scope.get(system) or snap_time
                obs = make_obs(meta, t, rid, interpret(kind, body), "sql:" + _topology(table),
                               location, {"table": table, **body}, system=system)
                if obs:
                    if rec.get("source_resource_id"):
                        obs.raw["source_resource_id"] = rec["source_resource_id"]
                    out.append(obs)
                    seen_systems.add(system)
        meta.explicit_coverage |= seen_systems
        if scope:
            for system, t in scope.items():
                meta.coverage[system] = t or snap_time
                meta.explicit_coverage.add(system)
        elif any(t.startswith("customer_data_") for t in tables):
            for system in CUSTOMER_SYSTEMS:
                meta.coverage[system] = snap_time
        else:
            for system in seen_systems:
                meta.coverage[system] = snap_time
        return out
    finally:
        con.close()


def _topology(table: str) -> str:
    if "replica" in table:
        return "replica"
    if "primary" in table:
        return "primary"
    return "inventory"
