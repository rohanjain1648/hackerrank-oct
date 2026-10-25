"""Operational logs in six syntaxes (key=value, JSON, bracketed, piped, syslog,
cloud-runtime). Lines are normalised to (time, service, trace, event, fields)
and grouped by trace id, so the lookup key logged by ``*_started`` is joined
to the resource and outcome logged by the matching ``*_completed`` event.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from ..textutil import is_null, parse_time, subject_keys, to_bool
from .common import FileMeta, make_obs
from .records import interpret_lookup

KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET = re.compile(r"\[(\w+)\] \[(\w+)\] \[([\w-]+)\] (\w+):(.*)$")
PIPED = re.compile(r"\| (\w+) \| (\w+) \| ([\w-]+) \| (\w+) \|(.*)$")
SYSLOG = re.compile(r"<\d+>1 \S+ (\w+) ([\w-]+) - - (.*)$")
KEY_FIELDS = ("lookup_key", "subject_key", "match_key", "scan_key", "subject_ref", "owner_key")
LINK_FIELDS = ("correlation_key", "subject_reference", "customer_reference", "subject_ref",
               "lookup_key", "customer_email", "requester_email", "owner_email", "email",
               "matched_value", "phone", "requester_phone", "email_hash")


def _kv(body: str) -> dict:
    out = {}
    for k, v in KV.findall(body):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    return out


def parse_line(line: str) -> dict | None:
    line = line.rstrip("\r\n")
    if len(line) < 21 or not line[:4].isdigit():
        return None
    ts = parse_time(line[:20])
    rest = line[21:]
    service = trace = event = None
    if rest.lstrip().startswith("{"):
        try:
            fields = json.loads(rest)
        except json.JSONDecodeError:
            return None
        service = fields.get("service") or fields.get("logger")
    else:
        m = BRACKET.match(rest) or PIPED.match(rest)
        if m:
            service, trace, event, body = m.group(2), m.group(3), m.group(4), m.group(5)
            fields = _kv(body)
        else:
            m = SYSLOG.match(rest)
            if m:
                service, trace, body = m.group(1), m.group(2), m.group(3)
                fields = _kv(body)
            else:
                fields = _kv(rest)
        if "message" in fields and "=" in fields["message"]:
            fields.update(_kv(fields["message"]))
        service = service or fields.get("service") or fields.get("function")
    trace = trace or fields.get("trace_id") or fields.get("requestId")
    event = event or fields.get("event")
    return {"ts": ts, "service": service, "trace": trace, "event": event, "fields": fields}


def _keys_from(fields: dict, names) -> set[str]:
    keys: set[str] = set()
    for f in names:
        v = fields.get(f)
        if not is_null(v):
            keys |= subject_keys(v)
    return keys


def _exposed(fields: dict) -> set[str]:
    out: set[str] = set()
    for f in ("email", "phone", "requester_email", "requester_phone", "owner_email",
              "customer_email", "email_hash", "matched_value"):
        v = fields.get(f)
        if not is_null(v):
            out |= subject_keys(v)
    for f in ("requester_name", "full_name", "display_name", "name"):
        v = fields.get(f)
        if not is_null(v):
            out.add("name:" + str(v).lower())
    return out


def _state(service: str, event: str, f: dict) -> dict | None:
    """Map one outcome event to observation state; None if it is not an outcome."""
    rid = f.get("resource_id", "")
    exposed = _exposed(f)
    if event == "record_lookup":
        return interpret_lookup(f.get("record_type"), f.get("result"), f.get("subject_reference"))
    if event == "profile_lookup_completed":
        absent = f.get("http_status") == "404" or f.get("rows") == "0"
        return {"presence": "ABSENT" if absent else "PRESENT", "exposed": exposed,
                "exposure": "IDENTIFIABLE" if exposed else None}
    if event == "index_query_completed":
        hit = f.get("result") == "hit" or (f.get("total_hits") not in (None, "0"))
        if f.get("result") == "miss":
            hit = False
        return {"presence": "PRESENT" if hit else "ABSENT", "exposed": exposed}
    if event in ("analytics_profile_read", "analytics_profile_read_completed"):
        ident = bool(exposed) or not is_null(f.get("customer_reference"))
        if f.get("direct_identifiers") not in (None, ""):
            ident = ident or f.get("direct_identifiers") != "0"
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE" if ident else "CLEAN", "exposed": exposed}
    if event == "analytics_identity_scan":
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE", "exposed": exposed}
    if event == "contact_lookup_completed":
        present = f.get("rows") not in ("0", None)
        return {"presence": "PRESENT" if present else "ABSENT", "exposure": "IDENTIFIABLE",
                "exposed": _keys_from(f, ("correlation_key",)), "category": "SECONDARY_IDENTIFIER"}
    if event == "message_pii_scan":
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE", "exposed": exposed,
                "category": "SUPPORT_MESSAGE"}
    if event == "attachment_pii_scan":
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE", "exposed": exposed,
                "category": "SUPPORT_ATTACHMENT", "note": f"file={f.get('file')}"}
    if event == "message_read":
        text = f.get("message_text") or ""
        ident = bool(subject_keys(text)) or ("@" in text)
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE" if ident else "CLEAN",
                "exposed": exposed, "category": "SUPPORT_MESSAGE"}
    if event == "support_ticket_read_completed":
        return {"presence": "PRESENT", "exposure": "IDENTIFIABLE" if exposed else "CLEAN", "exposed": exposed}
    if event == "contact_query_completed":
        present = f.get("rows") not in ("0", None)
        return {"presence": "PRESENT" if present else "ABSENT", "exposure": "IDENTIFIABLE",
                "exposed": _keys_from(f, ("lookup_key",))}
    if event == "deletion_marker_check":
        if to_bool(f.get("marker_found")) is False:
            return {"presence": "PRESENT", "note": "deletion marker missing"}
        return {"presence": "ABSENT", "note": "deletion marker found"}
    if event in ("export_download_check_completed", "export_metadata_read_completed"):
        avail = to_bool(f.get("available"))
        return {"presence": "PRESENT", "exposed": exposed, "note": f"available={avail}"}
    if event in ("connector_heartbeat", "connector_config_loaded", "connector_config_read_completed"):
        enabled = to_bool(f.get("enabled"))
        return {"presence": "PRESENT", "flow": "ACTIVE" if enabled else "DISABLED", "exposed": exposed}
    if event == "restore_candidate_evaluated":
        ok = to_bool(f.get("eligible")) and not to_bool(f.get("deletion_block"))
        return {"presence": "PRESENT", "restore": "ACTIVE" if ok else "BLOCKED"}
    if event == "restore_authorization_completed":
        allow = (f.get("decision") or "").lower() in ("allow", "allowed", "approve", "approved")
        return {"presence": "PRESENT", "restore": "ACTIVE" if allow else "BLOCKED"}
    if event == "restore_policy_read_completed":
        if to_bool(f.get("archive_present")) is False:
            return {"presence": "ABSENT", "restore": "BLOCKED"}
        ok = to_bool(f.get("restore_allowed"))
        return {"presence": "PRESENT", "restore": "ACTIVE" if ok else "BLOCKED", "exposed": exposed}
    if event == "audit_record_read_completed":
        present = f.get("rows") not in ("0",)
        note = f"retention_until={f['retention_until']}" if f.get("retention_until") else ""
        return {"presence": "PRESENT" if present else "ABSENT", "note": note}
    if event == "retained_order_loaded":
        return {"presence": "PRESENT", "note": f"reason={f.get('reason')}"}
    if rid and event and event.endswith("_completed"):
        return {"presence": "PRESENT", "exposed": exposed}
    return None


def parse_log(meta: FileMeta, path) -> list:
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            ev = parse_line(line)
            if ev:
                events.append(ev)
    trace_keys: dict[str, set[str]] = defaultdict(set)
    for ev in events:
        f = ev["fields"]
        if ev["trace"] and (ev["event"] or "").endswith("_started"):
            trace_keys[ev["trace"]] |= _keys_from(f, KEY_FIELDS)
    out = []
    for ev in events:
        f = ev["fields"]
        rid = f.get("resource_id")
        if not rid or (ev["event"] or "").endswith("_started"):
            continue
        state = _state(ev["service"] or "", ev["event"] or "", f)
        if state is None:
            continue
        keys = set(state.get("keys") or ()) | _keys_from(f, LINK_FIELDS)
        if ev["trace"]:
            keys |= trace_keys.get(ev["trace"], set())
        state["keys"] = keys
        state.setdefault("exposed", set())
        obs = make_obs(meta, ev["ts"], rid, state, "log:" + (ev["event"] or "event"),
                       ev["service"], {"trace": ev["trace"], **f},
                       system=f.get("system") if ev["event"] == "record_lookup" else None)
        if obs:
            out.append(obs)
    return out
