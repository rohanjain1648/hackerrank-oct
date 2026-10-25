"""Participant-authored evaluation cases.

A miniature, fully fictional dataset is written to a temporary folder in the
same formats as ``dataset/`` and audited end-to-end by the real agent. Each
case targets one failure mode that the public samples under-represent; the
expected answers below were derived by hand from deletion_rules.csv, not by
running the agent.

custom_01  request uses a HISTORICAL email; profile survives only in a read
           replica (replication drift); search entry keyed by the old address.
custom_02  look-alike decoy ("ben.jr@...") hit in search must NOT be treated as
           the subject's; profile 404 proven by a trace-joined lookup log.
custom_03  evidence captured AFTER audit_at says the connector is active again;
           the answer must use only the earlier capture (COMPLETE).
custom_04  account tombstone still shows the person's name and email: keep the
           stable user_id, redact the rest (REDACT, not DELETE).
custom_05  "send me a copy ... don't delete anything" -> NOT_APPLICABLE.
custom_06  restorable backup + a profile that carries no identifier at all:
           the profile is tied to the case only through the deletion-worker
           activity in the case's workflow window, competing with custom_03's
           identifier-less profile in the same window.
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from pathlib import Path

REQ = "2026-05-01T10:00:00Z"
REP = "2026-05-02T12:00:00Z"
AUDIT = "2026-05-10T12:00:00Z"

IDENTITIES = [
    ("user_901", "Ana Testa", "ana.new@example.com", "+351 912 000 901"),
    ("user_902", "Ben Okoro", "ben@example.com", "+234 803 000 902"),
    ("user_903", "Cara Lind", "cara@example.com", "+46 70 000 0903"),
    ("user_904", "Dev Rao", "dev@example.com", "+91 90000 00904"),
    ("user_905", "Eli Moss", "eli@example.com", "+1 (212) 555-0905"),
    ("user_906", "Fay Noor", "fay@example.com", "+971 50 000 0906"),
]

REQUESTS = [
    ("custom_01", "ana.old@example.com", "COMPLETE", "Please delete my account. I used this older address before."),
    ("custom_02", "ben@example.com", "COMPLETE", "Please erase the account linked to this email address."),
    ("custom_03", "cara@example.com", "PARTIALLY_COMPLETE", "I would like my account and personal data deleted."),
    ("custom_04", "dev@example.com", "COMPLETE", "Remove my name and contact details; an internal reference is fine."),
    ("custom_05", "eli@example.com", "NOT_STARTED",
     "Could you send me a copy of my data? I still use the account, so please don't delete anything."),
    ("custom_06", "fay@example.com", "COMPLETE", "Please permanently delete the account associated with this email address."),
]


def _sha(email):
    import hashlib

    return hashlib.sha256(email.encode()).hexdigest()


def _v(step_no, phase, system, rid, op, state, ev=""):
    return {"step_no": step_no, "phase": phase, "system_id": system, "resource_id": rid,
            "operation": op, "state": state, "evidence_id": ev}


EXPECTED = {
    "custom_01": ("INCOMPLETE", [
        _v(1, "FINDING", "account_service", "account_9001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_c1"),
        _v(2, "FINDING", "search_service", "search_9001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_c1"),
        _v(3, "REMEDIATION", "account_service", "account_9001", "DELETE_RECORD", "ABSENT"),
        _v(4, "REMEDIATION", "search_service", "search_9001", "DELETE_RECORD", "ABSENT"),
        _v(5, "VERIFICATION", "account_service", "account_9001", "VERIFY_RECORD_ABSENT", "ABSENT"),
        _v(6, "VERIFICATION", "search_service", "search_9001", "VERIFY_RECORD_ABSENT", "ABSENT"),
        _v(7, "VERIFICATION", "security_service", "audit_record_9001", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ]),
    "custom_02": ("COMPLETE", [
        _v(1, "VERIFICATION", "account_service", "account_9002", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_c2"),
        _v(2, "VERIFICATION", "security_service", "audit_record_9002", "VERIFY_RECORD_PRESENT", "PRESENT", "evidence_c2"),
    ]),
    "custom_03": ("COMPLETE", [
        _v(1, "VERIFICATION", "account_service", "account_9003", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_c3a"),
        _v(2, "VERIFICATION", "integration_service", "data_flow_9003", "VERIFY_DATA_FLOW_DISABLED", "DISABLED",
           "evidence_c3a"),
    ]),
    "custom_04": ("INCOMPLETE", [
        _v(1, "FINDING", "identity_service", "tombstone_9004", "REPORT_EXPOSED_PII", "PRESENT", "evidence_c4"),
        _v(2, "REMEDIATION", "identity_service", "tombstone_9004", "REDACT_RECORD", "REDACTED"),
        _v(3, "VERIFICATION", "account_service", "account_9004", "VERIFY_RECORD_ABSENT", "ABSENT"),
        _v(4, "VERIFICATION", "security_service", "audit_record_9004", "VERIFY_RECORD_PRESENT", "PRESENT"),
        _v(5, "VERIFICATION", "identity_service", "tombstone_9004", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ]),
    "custom_05": ("NOT_APPLICABLE", []),
    "custom_06": ("INCOMPLETE", [
        _v(1, "FINDING", "backup_service", "backup_9006", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_c6"),
        _v(2, "REMEDIATION", "backup_service", "backup_9006", "BLOCK_RESTORE", "BLOCKED"),
        _v(3, "VERIFICATION", "account_service", "account_9006", "VERIFY_RECORD_ABSENT", "ABSENT"),
        _v(4, "VERIFICATION", "security_service", "audit_record_9006", "VERIFY_RECORD_PRESENT", "PRESENT"),
        _v(5, "VERIFICATION", "backup_service", "backup_9006", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
    ]),
}


def _write_csv(path: Path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _rec(table, rid, system, collection, body):
    return (f"INSERT INTO \"customer_data_{table}\" VALUES('{rid}','{system}','{collection}',"
            f"'{json.dumps(body)}','2026-05-09T06:00:00Z');")


def build_dataset(root: Path, rules_csv: Path) -> Path:
    ev = root / "evidence"
    for sub in ("databases", "logs", "records"):
        (ev / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(rules_csv, root / "deletion_rules.csv")
    _write_csv(root / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
               [(u, n, e, p, _sha(e), "2026-01-01T00:00:00Z") for u, n, e, p in IDENTITIES])
    _write_csv(root / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
               [("audit_c1", "user_901", "2026-02-01T00:00:00Z", "email", "ana.old@example.com", "ana.new@example.com")])
    _write_csv(root / "deletion_requests.csv",
               ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
               [(c, e, REQ, s, REP, AUDIT, t) for c, e, s, t in REQUESTS])
    _write_csv(root / "sample_deletion_requests.csv",
               ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"], [])
    # Profiles of custom_03 / custom_06 carry no identifier anywhere: only the
    # deletion-worker activities (same window, different reported results) tie them.
    _write_csv(root / "system_activity.csv",
               ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                "reported_result", "details"],
               [("act_c3", "2026-05-02T10:50:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9003",
                 "PARTIAL", "Deletion worker submitted primary profile account_9003 to account_service."),
                ("act_c6", "2026-05-02T11:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9006",
                 "SUCCESS", "Deletion worker submitted primary profile account_9006 to account_service."),
                ("act_c6b", "2026-05-02T13:00:00Z", "CHECK_RESTORE_ACCESS", "deletion_worker", "backup_service",
                 "backup_9006", "SUCCESS", "Restore-access check confirmed that archive backup_9006 can still be restored.")])

    # custom_01 - read-replica drift, keyed by the historical address
    tables = """BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
COMMIT;
"""
    acc = {"resource_id": "account_9001", "customer_reference": "user_901", "full_name": "Ana Testa",
           "email": "ana.new@example.com", "phone": "+351 912 000 901", "account_status": "ACTIVE"}
    srch = {"resource_id": "search_9001", "index_name": "customer_directory_v3",
            "document": {"display_name": None, "email": "ana.old@example.com", "customer_reference": None}}
    aud = {"resource_id": "audit_record_9001", "subject_reference": "user_901", "event_name": "account_deletion_reviewed"}
    inserts = [
        _rec("read_replica", "account_9001", "account_service", "customer_profiles", acc),
        _rec("primary", "search_9001", "search_service", "customer_search_documents", srch),
        _rec("primary", "audit_record_9001", "security_service", "security_audit_events", aud),
    ]
    (ev / "databases" / "c1_read_replica.sql").write_text(
        "-- Captured at: 2026-05-09T06:00:00Z\n" + tables + "\n".join(inserts) + "\n", encoding="utf-8")

    # custom_02 - trace-joined lookups plus a look-alike decoy
    (ev / "logs" / "c2_lookup.log").write_text("\n".join([
        "2026-05-09T07:00:00Z level=DEBUG service=account_service trace_id=t-1 event=profile_lookup_started lookup_key=ben@example.com",
        "2026-05-09T07:00:01Z level=INFO service=account_service trace_id=t-1 event=profile_lookup_completed resource_id=account_9002 rows=0 http_status=404",
        "2026-05-09T07:01:00Z level=DEBUG service=security_service trace_id=t-2 event=audit_record_query_started subject_ref=user_902",
        "2026-05-09T07:01:01Z level=DEBUG service=security_service trace_id=t-2 resource_id=audit_record_9002 event=audit_record_read_completed rows=1",
        '2026-05-09T07:02:00Z level=DEBUG service=search_service trace_id=t-3 event=index_query_started match_key="ben.jr@example.com"',
        "2026-05-09T07:02:01Z level=INFO service=search_service trace_id=t-3 resource_id=search_9099 event=index_query_completed result=hit",
    ]) + "\n", encoding="utf-8")

    # custom_03 - an eligible capture and a later one that must be ignored
    def svc(flow_enabled, when):
        return {"exported_at": when, "region": "global", "source": "custom", "service_exports": {
            "account_service": {"profile_lookups": [{"observed_at": when, "profile": None,
                                                     "resource_id": "account_9003", "status_code": 404}]},
            "integration_service": {"connectors": [{
                "connector_name": "customer_events_connector", "customer_email": None,
                "customer_reference": "user_903", "enabled": flow_enabled,
                "last_delivery_status": "delivered" if flow_enabled else "stopped",
                "observed_at": when, "resource_id": "data_flow_9003"}]}}}
    (ev / "records" / "c3_before.json").write_text(json.dumps(svc(False, "2026-05-09T08:00:00Z")), encoding="utf-8")
    (ev / "records" / "c3_after.json").write_text(json.dumps(svc(True, "2026-05-12T08:00:00Z")), encoding="utf-8")

    # custom_04 - hand-over note with an exposed tombstone
    (ev / "records" / "c4_handover.txt").write_text("\n".join([
        "CUSTOMER PLATFORM - SHIFT HANDOVER",
        "- 2026-05-09T09:00:00Z | account_service | account_9004 | USER_PROFILE | NOT_FOUND | dev@example.com",
        "- 2026-05-09T09:00:00Z | identity_service | tombstone_9004 | ACCOUNT_TOMBSTONE | IDENTIFIABLE | dev@example.com",
        "- 2026-05-09T09:00:00Z | security_service | audit_record_9004 | SECURITY_AUDIT_LOG | FOUND | user_904",
    ]) + "\n", encoding="utf-8")

    # custom_06 - inventory snapshot: restorable backup, profile absent
    (ev / "databases" / "c6_inventory.sql").write_text("""BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
CREATE TABLE backup_restore_entries (resource_id TEXT PRIMARY KEY, customer_reference TEXT, customer_email TEXT, archive_key TEXT NOT NULL, restore_allowed INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE security_audit_records (resource_id TEXT PRIMARY KEY, subject_reference TEXT NOT NULL, event_code TEXT NOT NULL, retention_until TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES ('account_service', '2026-05-09T10:00:00Z');
INSERT INTO snapshot_scope VALUES ('backup_service', '2026-05-09T10:00:00Z');
INSERT INTO backup_restore_entries VALUES ('backup_9006', NULL, 'fay@example.com', 'archive/backup_9006.tar.zst', 1, '2026-05-09T09:00:00Z');
INSERT INTO security_audit_records VALUES ('audit_record_9006', 'user_906', 'ACCOUNT_DELETION_RECORDED', '2031-12-31T23:59:59Z', '2026-05-09T09:00:00Z');
COMMIT;
""", encoding="utf-8")

    _write_csv(root / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], [
        ("evidence_c1", "evidence/databases/c1_read_replica.sql", "custom_replica_capture", "2026-05-09T06:00:00Z"),
        ("evidence_c2", "evidence/logs/c2_lookup.log", "custom_readback", "2026-05-09T07:05:00Z"),
        ("evidence_c3a", "evidence/records/c3_before.json", "custom_export", "2026-05-09T08:00:00Z"),
        ("evidence_c3b", "evidence/records/c3_after.json", "custom_export", "2026-05-12T08:00:00Z"),
        ("evidence_c4", "evidence/records/c4_handover.txt", "custom_handover", "2026-05-09T09:00:00Z"),
        ("evidence_c6", "evidence/databases/c6_inventory.sql", "custom_inventory", "2026-05-09T10:00:00Z"),
    ])
    return root


def run_custom_cases(rules_csv: Path) -> list[dict]:
    from audit_agent.agent import DeletionAuditAgent, load_requests

    tmp = Path(tempfile.mkdtemp(prefix="ghost_custom_"))
    try:
        root = build_dataset(tmp, rules_csv)
        agent = DeletionAuditAgent(root, log=lambda *a: None)
        reqs = load_requests(root / "deletion_requests.csv")
        results = {r.case_id: r for r in agent.run(reqs, context_requests=reqs)}
        out = []
        for cid, (status, steps) in EXPECTED.items():
            r = results[cid]
            got = [s.as_dict(i + 1) for i, s in enumerate(r.steps)]
            out.append({"case_id": cid, "expected_status": status, "status": r.status,
                        "status_ok": r.status == status, "steps_ok": got == steps,
                        "expected": steps, "got": got})
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
