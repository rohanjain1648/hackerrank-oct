"""Deletion policy (deletion_rules.csv) and the step vocabulary of the contract."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
OPERATION_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"],
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"],
}
OPERATION_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# Treatment -> (finding op, remediation op, verification op)
TREATMENTS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    # Processes rather than records: they must be switched off, not deleted.
    "DISABLE_FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK_RESTORE": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


@dataclass
class Rule:
    rule_id: str
    category: str
    condition: str
    action: str


class Policy:
    def __init__(self, rules_csv: Path):
        self.rules: dict[str, Rule] = {}
        with open(rules_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rule = Rule(row["rule_id"], row["data_category"].strip(), row["condition"].strip(),
                            row["required_action"].strip().upper())
                self.rules[rule.category] = rule

    def treatment(self, category: str) -> tuple[str, Rule | None]:
        """Map a data category to the treatment the plan must enforce."""
        if category == "DATA_FLOW":
            return "DISABLE_FLOW", None
        rule = self.rules.get(category)
        if rule is None:
            return "DELETE", None
        if category == "BACKUP_ARCHIVE":
            # rule_14: the archive is retained, but only while restore access is blocked.
            return "BLOCK_RESTORE", rule
        if category == "ACCOUNT_TOMBSTONE":
            # rule_15: keep the stable user_id, remove every direct identifier.
            return "REDACT", rule
        return rule.action, rule


def step_sort_key(step) -> tuple:
    phase_idx = PHASES.index(step.phase)
    op_idx = OPERATION_ORDER[step.phase].index(step.operation)
    return (phase_idx, op_idx, step.system_id, step.resource_id)
