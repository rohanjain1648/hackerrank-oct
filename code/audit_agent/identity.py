"""Identity resolution: current snapshot plus historical identifiers."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import Person
from .textutil import email_hash, norm_email, norm_phone, parse_time


class IdentityIndex:
    def __init__(self, identities_csv: Path, audit_log_csv: Path):
        self.people: dict[str, Person] = {}
        self.by_key: dict[str, str] = {}
        self.audit_rows: list[dict] = []
        with open(identities_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                email = norm_email(row["email"]) or ""
                person = Person(
                    user_id=row["user_id"].strip(),
                    name=row["name"].strip(),
                    email=email,
                    phone=row["phone"].strip(),
                    email_hash=row["email_hash"].strip().lower(),
                    updated_at=parse_time(row["updated_at"]),
                )
                if email:
                    person.emails.add(email)
                    person.hashes.add(email_hash(email))
                if person.email_hash:
                    person.hashes.add(person.email_hash)
                phone = norm_phone(person.phone)
                if phone:
                    person.phones.add(phone)
                self.people[person.user_id] = person
        with open(audit_log_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                self.audit_rows.append(row)
                person = self.people.get(row["user_id"].strip())
                if person is None:
                    continue
                field_name = row["field_name"].strip()
                entry = {
                    "audit_id": row["audit_id"],
                    "time": parse_time(row["event_time"]),
                    "field": field_name,
                    "old": row["old_value"],
                    "new": row["new_value"],
                }
                person.history.append(entry)
                for value in (row["old_value"], row["new_value"]):
                    if field_name == "email":
                        email = norm_email(value)
                        if email:
                            person.emails.add(email)
                            person.hashes.add(email_hash(email))
                    elif field_name == "email_hash" and value:
                        person.hashes.add(value.strip().lower())
                    elif field_name == "phone":
                        phone = norm_phone(value)
                        if phone:
                            person.phones.add(phone)
        for person in self.people.values():
            for key in person.keys:
                self.by_key.setdefault(key, person.user_id)

    def resolve_email(self, email: str) -> Person | None:
        key = norm_email(email)
        if not key:
            return None
        user_id = self.by_key.get(key)
        return self.people.get(user_id) if user_id else None

    def owner_of(self, key: str) -> str | None:
        """Return the user that a normalised link key belongs to, if any."""
        return self.by_key.get(key)

    def email_audit_ids(self, person: Person, email: str) -> list[str]:
        """audit_ids that prove ``email`` was once one of the person's identifiers."""
        email = norm_email(email) or ""
        return [
            h["audit_id"]
            for h in person.history
            if h["field"] == "email" and email in {norm_email(h["old"]), norm_email(h["new"])}
        ]
