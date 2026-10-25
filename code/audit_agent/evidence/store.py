"""Evidence store: manifest-driven ingestion and indexes."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from ..models import Observation, default_system
from ..textutil import is_user_ref, parse_time
from .common import FileMeta
from .json_files import parse_json
from .log_files import parse_log
from .media import image_rows, interpret_document, pdf_rows
from .sql_files import parse_sql
from .text_files import parse_text

CATALOG_RE = re.compile(r"linked (\S+) \(([^)]+)\) to (\S+?) and (user_\d+)")
ATTACH_RE = re.compile(r"confirmed (\S+) \(([^)]+)\) belongs only to (\S+?) and (user_\d+)")


class EvidenceStore:
    def __init__(self):
        self.files: dict[str, FileMeta] = {}
        self.observations: list[Observation] = []
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.by_key: dict[str, set[str]] = defaultdict(set)
        self.aliases: dict[str, str] = {}
        self.catalog: list[dict] = []
        self.media_engines: dict[str, str] = {}

    def add(self, obs_list):
        for obs in obs_list:
            self.observations.append(obs)
            self.by_resource[obs.resource_id].append(obs)
            for k in obs.keys:
                self.by_key[k].add(obs.resource_id)

    def resources_for_keys(self, keys) -> set[str]:
        out: set[str] = set()
        for k in keys:
            out |= self.by_key.get(k, set())
        return out


def _catalog_entries(activity) -> list[dict]:
    entries = []
    for act in activity.rows:
        if act.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        m = CATALOG_RE.search(act.details) or ATTACH_RE.search(act.details)
        if not m:
            continue
        rid, alias, email, user = m.groups()
        entries.append({
            "activity_id": act.activity_id, "time": act.event_time, "resource_id": rid,
            "alias": alias, "email": email.rstrip(".").lower(), "user_id": user,
            "system": act.target_system, "exclusive": "belongs only" in act.details,
        })
    return entries


def load_evidence(dataset_dir: Path, activity, usage=None, vision=None, log=print) -> EvidenceStore:
    store = EvidenceStore()
    store.catalog = _catalog_entries(activity)
    with open(dataset_dir / "evidence_files.csv", encoding="utf-8", newline="") as fh:
        manifest = list(csv.DictReader(fh))
    for row in manifest:
        path = dataset_dir / row["file_path"]
        meta = FileMeta(
            evidence_id=row["file_id"], path=str(path), origin=row["origin"],
            captured_at=parse_time(row["captured_at"]), kind=path.suffix.lower().lstrip("."),
        )
        store.files[meta.evidence_id] = meta
        suffix = path.suffix.lower()
        if suffix == ".json":
            obs = parse_json(meta, path)
        elif suffix == ".sql":
            obs = parse_sql(meta, path)
        elif suffix == ".log":
            obs = parse_log(meta, path)
        elif suffix == ".txt":
            obs = parse_text(meta, path)
        elif suffix in (".pdf", ".png", ".jpg", ".jpeg"):
            if suffix == ".pdf":
                rows, engine = pdf_rows(path), "pdfplumber"
            else:
                rows, engine = image_rows(path, usage=usage, vision=vision)
            store.media_engines[meta.evidence_id] = engine
            aliases = {}
            for entry in store.catalog:
                if is_user_ref(entry["alias"]):
                    # A bare user reference only names a record on screens of that system.
                    if meta.origin.split("_")[0] in entry["system"]:
                        aliases[entry["alias"]] = entry["resource_id"]
                elif entry["alias"] != entry["resource_id"]:
                    aliases[entry["alias"]] = entry["resource_id"]
            obs = interpret_document(meta, rows, aliases)
        else:
            obs = []
        store.add(obs)
    log(f"[evidence] {len(store.files)} files -> {len(store.observations)} observations, "
        f"{len(store.by_resource)} resources")
    return store
