"""Format-independent interpretation of a single record.

JSON bundles, SQL snapshots and log traces all describe the same handful of
record types with slightly different field names. Each interpreter returns
the state fields of an Observation (presence/exposure/flow/restore, link keys
and the identifiers the record still exposes).
"""

from __future__ import annotations

import re

from ..textutil import EMAIL_RE, is_null, norm_phone, subject_keys, to_bool

IDENTIFIER_FIELDS = (
    "email", "owner_email", "customer_email", "requester_email", "uploaded_by",
    "phone", "owner_phone", "requester_phone",
    "email_hash",
    "full_name", "name", "display_name", "requester_name",
)
REFERENCE_FIELDS = ("customer_reference", "subject_reference", "user_id", "subject_selector")


def _identifiers(rec: dict) -> tuple[set[str], set[str]]:
    """Return (link keys, exposed identifiers) found in a flat record."""
    keys: set[str] = set()
    exposed: set[str] = set()
    for f in IDENTIFIER_FIELDS:
        v = rec.get(f)
        if is_null(v):
            continue
        if f in ("full_name", "name", "display_name", "requester_name"):
            exposed.add("name:" + str(v).strip().lower())
            continue
        k = subject_keys(v)
        if f.endswith("phone") and not k:
            p = norm_phone(v)
            k = {"tel:" + p} if p else set()
        keys |= k
        exposed |= k
    for f in REFERENCE_FIELDS:
        v = rec.get(f)
        if not is_null(v):
            k = subject_keys(v)
            keys |= k
            # A user reference alone is the permitted stable key; emails used as
            # selectors are still direct identifiers.
            exposed |= {x for x in k if not x.startswith("user_")}
    return keys, exposed


def _text_identifiers(text: str) -> set[str]:
    out = set()
    for m in EMAIL_RE.findall(text or ""):
        out |= subject_keys(m)
    return out


def interpret(kind: str, rec: dict) -> dict:
    """Interpret ``rec`` as a record of resource ``kind`` (account, search, ...)."""
    keys, exposed = _identifiers(rec)
    out: dict = {"keys": keys, "exposed": exposed}

    if kind == "account":
        status = str(rec.get("account_status") or "").upper()
        out["presence"] = "ABSENT" if status in {"DELETED", "PURGED", "REMOVED"} else "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if exposed else "CLEAN"
    elif kind == "search":
        doc = rec.get("document") if isinstance(rec.get("document"), dict) else None
        if doc is not None:
            k2, e2 = _identifiers(doc)
            out["keys"] |= k2
            out["exposed"] |= e2
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if out["exposed"] else "CLEAN"
    elif kind == "analytics":
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if (exposed or _has_user_ref(rec)) else "CLEAN"
    elif kind == "support":
        text = rec.get("message_text") or rec.get("message_excerpt") or ""
        text_ids = _text_identifiers(text)
        out["keys"] |= text_ids
        out["exposed"] |= text_ids
        if rec.get("file_name") or rec.get("storage_class") == "ticket_attachment":
            out["category"] = "SUPPORT_ATTACHMENT"
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if out["exposed"] else "CLEAN"
    elif kind == "export":
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if exposed else "CLEAN"
        avail = to_bool(rec.get("download_available"))
        if avail is not None:
            out["note"] = f"download_available={avail}"
    elif kind == "data_flow":
        enabled = to_bool(rec.get("enabled"))
        out["presence"] = "PRESENT"
        out["flow"] = "ACTIVE" if enabled else "DISABLED"
    elif kind == "backup":
        allowed = to_bool(rec.get("restore_allowed"))
        out["presence"] = "PRESENT"
        out["restore"] = "ACTIVE" if allowed else "BLOCKED"
    elif kind == "audit_record":
        out["presence"] = "PRESENT"
        if rec.get("retention_until"):
            out["note"] = f"retention_until={rec['retention_until']}"
    elif kind == "tombstone":
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if exposed else "CLEAN"
    elif kind == "legal_hold":
        out["presence"] = "PRESENT"
        if rec.get("retained_until"):
            out["note"] = f"retained_until={rec['retained_until']}"
    else:
        out["presence"] = "PRESENT"
        out["exposure"] = "IDENTIFIABLE" if exposed else "CLEAN"
    return out


def _has_user_ref(rec: dict) -> bool:
    return any(not is_null(rec.get(f)) for f in ("user_id", "customer_reference"))


# --- generic "record lookup" rows: (system, resource_id, record_type, result, subject) ---

RESULT_STATE = {
    "FOUND": {"presence": "PRESENT"},
    "PRESENT": {"presence": "PRESENT"},
    "NOT_FOUND": {"presence": "ABSENT"},
    "ABSENT": {"presence": "ABSENT"},
    "DELETED": {"presence": "ABSENT"},
    "IDENTIFIABLE": {"presence": "PRESENT", "exposure": "IDENTIFIABLE"},
    "EXPOSED": {"presence": "PRESENT", "exposure": "IDENTIFIABLE"},
    "ANONYMIZED": {"presence": "PRESENT", "exposure": "CLEAN"},
    "REDACTED": {"presence": "PRESENT", "exposure": "CLEAN"},
    "RETAINED": {"presence": "PRESENT"},
    "DISABLED": {"presence": "PRESENT", "flow": "DISABLED"},
    "BLOCKED": {"presence": "PRESENT", "restore": "BLOCKED"},
}


def interpret_lookup(record_type: str, result: str, subject: str | None) -> dict:
    record_type = (record_type or "").strip().upper()
    result = (result or "").strip().upper()
    out: dict = {"keys": set(), "exposed": set(), "category": record_type or None}
    if subject and not re.search(r"no direct identifier", subject, re.I):
        out["keys"] = subject_keys(subject)
    if result == "ACTIVE":
        out["presence"] = "PRESENT"
        if record_type == "BACKUP_ARCHIVE":
            out["restore"] = "ACTIVE"
        else:
            out["flow"] = "ACTIVE"
    else:
        out.update(RESULT_STATE.get(result, {}))
    if out.get("exposure") == "IDENTIFIABLE" or (
        result == "FOUND" and record_type in {"SUPPORT_ATTACHMENT", "USER_EXPORT", "SEARCH_INDEX_ENTRY", "USER_PROFILE"}
    ):
        out["exposed"] = {k for k in out["keys"] if not k.startswith("user_")}
    return out
