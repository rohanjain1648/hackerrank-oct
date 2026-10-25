"""Small normalisation helpers shared by every parser."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_REF_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|marketing|object|cache|email_delivery)_\d+\b"
)

NULLS = {"", "null", "none", "nil", "-", "—"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value or value.lower() in NULLS:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in NULLS)


def norm_email(value: str | None) -> str | None:
    if is_null(value):
        return None
    value = str(value).strip().lower()
    return value if "@" in value else None


def norm_phone(value: str | None) -> str | None:
    if is_null(value):
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits if len(digits) >= 7 else None


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def is_user_ref(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"user_\d+", value.strip()) is not None


def is_hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value.strip()) is not None


def to_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "enabled", "allowed"}:
        return True
    if text in {"0", "false", "no", "n", "disabled", "denied"}:
        return False
    return None


def subject_keys(value) -> set[str]:
    """Normalise a raw identifier into the link keys it can match.

    Emails are lower-cased, user references kept verbatim, SHA-256 hashes kept,
    phones reduced to digits. Anything else (resource ids, free text) yields
    nothing, so a lookup by resource id never links a record to a person.
    """
    keys: set[str] = set()
    if is_null(value):
        return keys
    text = str(value).strip()
    if "@" in text:
        email = norm_email(text)
        if email:
            keys.add(email)
    elif is_user_ref(text):
        keys.add(text)
    elif is_hash(text.lower()):
        keys.add(text.lower())
    elif re.fullmatch(r"[+()\d\s.\-]{7,}", text):
        phone = norm_phone(text)
        if phone:
            keys.add("tel:" + phone)
    return keys
