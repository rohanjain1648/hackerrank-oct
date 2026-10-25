"""JSON evidence: service exports, snapshot bundles, record ledgers, collections."""

from __future__ import annotations

import json

from ..models import resource_kind
from ..textutil import parse_time, subject_keys, to_bool
from .common import FileMeta, make_obs
from .records import interpret, interpret_lookup


def parse_json(meta: FileMeta, path) -> list:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if "service_exports" in data:
        return _service_exports(meta, data)
    if "snapshots" in data:
        return _snapshots(meta, data)
    if "records" in data:
        return _record_list(meta, data)
    return []


def _lookup_row(meta, rec, source, location, default_time=None):
    state = interpret_lookup(rec.get("record_type"), rec.get("result"),
                             rec.get("subject_reference") or rec.get("lookup_value"))
    t = parse_time(rec.get("recorded_at")) or default_time
    return make_obs(meta, t, rec.get("resource_id"), state, source, location, rec, system=rec.get("system"))


def _record_list(meta: FileMeta, data: dict) -> list:
    out = []
    default_time = parse_time(data.get("captured_at") or data.get("exported_at"))
    collection = data.get("collection") or data.get("origin") or ""
    for rec in data.get("records", []):
        if "record_type" in rec and "result" in rec:
            obs = _lookup_row(meta, rec, "json:ledger", collection, default_time)
        else:
            kind = resource_kind(rec.get("resource_id", ""))
            obs = make_obs(meta, default_time, rec.get("resource_id"), interpret(kind, rec),
                           "json:collection", collection, rec)
            if kind:
                meta.coverage.setdefault(obs.system if obs else kind, default_time)
        if obs:
            out.append(obs)
    return out


def _snapshots(meta: FileMeta, data: dict) -> list:
    out = []
    for snap in data.get("snapshots", []):
        t = parse_time(snap.get("captured_at")) or meta.captured_at
        store = snap.get("data_store", "")
        collection = snap.get("collection", "")
        records = snap.get("records", [])
        if collection == "operational_record_history":
            for rec in records:
                obs = _lookup_row(meta, rec, "json:history", f"{store}/{collection}")
                if obs:
                    out.append(obs)
            continue
        # A snapshot enumerates the collection for the subjects in scope.
        prev = meta.coverage.get(store)
        meta.coverage[store] = max(prev, t) if prev else t
        meta.explicit_coverage.add(store)
        for rec in records:
            kind = resource_kind(rec.get("resource_id", ""))
            obs = make_obs(meta, t, rec.get("resource_id"), interpret(kind, rec),
                           "json:snapshot", f"{store}/{collection}", rec)
            if obs:
                out.append(obs)
    return out


def _service_exports(meta: FileMeta, data: dict) -> list:
    out = []
    for svc, groups in data.get("service_exports", {}).items():
        for group, items in groups.items():
            for it in items:
                t = parse_time(it.get("observed_at")) or meta.captured_at
                rid = it.get("resource_id")
                kind = resource_kind(rid or "")
                if svc == "account_service":
                    status = it.get("status_code")
                    prof = it.get("profile") if isinstance(it.get("profile"), dict) else None
                    if prof is not None and status in (200, "200"):
                        state = interpret("account", prof)
                    else:
                        state = {"presence": "ABSENT", "keys": set(), "exposed": set()}
                    obs = make_obs(meta, t, rid, state, "json:svcexp", f"{svc}/{group}", it)
                    if obs:
                        out.append(obs)
                elif svc == "search_service":
                    hits = it.get("hits") or {}
                    docs = hits.get("documents") or []
                    qkeys = subject_keys(it.get("query"))
                    if not docs:
                        state = {"presence": "ABSENT", "keys": qkeys, "exposed": set()}
                        obs = make_obs(meta, t, rid, state, "json:svcexp", f"{svc}/{group}", it)
                        if obs:
                            out.append(obs)
                    for doc in docs:
                        state = interpret("search", doc)
                        obs = make_obs(meta, t, doc.get("resource_id") or rid, state,
                                       "json:svcexp", f"{svc}/{group}", it)
                        if obs:
                            obs.raw = {"query": it.get("query"), **doc}
                            out.append(obs)
                else:
                    rec = dict(it)
                    if svc == "integration_service":
                        rec["enabled"] = to_bool(it.get("enabled"))
                    state = interpret(kind, rec) if kind else {}
                    obs = make_obs(meta, t, rid, state, "json:svcexp", f"{svc}/{group}", it)
                    if obs:
                        out.append(obs)
    return out
