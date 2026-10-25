"""PDF documents and application screenshots.

PDF text comes from pdfplumber. Screenshots go through local neural OCR
(RapidOCR, ONNX) and, when ANTHROPIC_API_KEY is set, a Claude vision pass that
returns the same row-structured text. Results are cached by file hash, so a
re-run never repeats OCR or model calls.

The extracted text is turned into observations by ``interpret_document``:
resource ids (or catalogue aliases such as a statement number or attachment
file name) are located row by row, identifiers on the same row become link
keys, and screen vocabulary ("Healthy", "Ready", "Retained reference",
"[REDACTED]", ...) decides the record state.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..models import resource_kind
from ..paths import CACHE_DIR
from ..textutil import EMAIL_RE, RESOURCE_RE, USER_REF_RE, subject_keys
from .common import FileMeta, make_obs

PHONE_RE = re.compile(r"\+\d[\d\s().\-]{7,}\d")
_OCR_ENGINE = None


def _sha1(path: Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def pdf_rows(path: Path) -> list[str]:
    try:
        import pdfplumber

        rows: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text(layout=False) or ""
                rows.extend(line for line in text.splitlines() if line.strip())
        return rows
    except Exception:  # pragma: no cover - fallback path
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [ln for page in reader.pages for ln in (page.extract_text() or "").splitlines() if ln.strip()]


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def image_rows(path: Path, usage=None, vision=None) -> tuple[list[str], str]:
    """Return text rows for a screenshot and the engine that produced them."""
    cache = CACHE_DIR / "media"
    cache.mkdir(parents=True, exist_ok=True)
    digest = _sha1(path)
    if vision is not None:
        cached = cache / f"{digest}.vision.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))["rows"], "claude-vision(cache)"
        rows = vision(path)
        if rows:
            cached.write_text(json.dumps({"file": Path(path).name, "rows": rows}, indent=1), encoding="utf-8")
            return rows, "claude-vision"
    cached = cache / f"{digest}.ocr.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))["rows"], "rapidocr(cache)"
    result, _ = _ocr_engine()(str(path))
    rows = _group_rows(result or [])
    cached.write_text(json.dumps({"file": Path(path).name, "rows": rows}, indent=1), encoding="utf-8")
    if usage is not None:
        usage.record_local("rapidocr-onnxruntime", "RapidOCR (PP-OCR ONNX)", units=1)
    return rows, "rapidocr"


def _group_rows(result) -> list[str]:
    """Group OCR fragments whose vertical centres align into table rows."""
    items = []
    for box, text, conf in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append(((min(ys) + max(ys)) / 2, min(xs), text.strip(), max(ys) - min(ys)))
    items.sort()
    rows: list[list] = []
    for yc, x, text, h in items:
        if rows and abs(rows[-1][0] - yc) <= max(6.0, 0.45 * h):
            rows[-1][1].append((x, text))
        else:
            rows.append([yc, [(x, text)]])
    return [" | ".join(t for _, t in sorted(parts)) for _, parts in rows]


STATE_WORDS = {
    "inactive": ("flow", "DISABLED"), "disabled": ("flow", "DISABLED"), "paused": ("flow", "DISABLED"),
    "stopped": ("flow", "DISABLED"),
}


def interpret_document(meta: FileMeta, rows: list[str], aliases: dict[str, str]) -> list:
    """Turn document/screenshot rows into observations."""
    text = "\n".join(rows)
    meta.text = text
    lowered = text.lower()
    doc_keys: set[str] = set()
    for m in EMAIL_RE.findall(text):
        doc_keys |= subject_keys(m)
    found: dict[str, dict] = {}

    def note(rid: str, row: str):
        entry = found.setdefault(rid, {"rows": [], "keys": set()})
        entry["rows"].append(row)
        for m in EMAIL_RE.findall(row):
            entry["keys"] |= subject_keys(m)
        for m in USER_REF_RE.findall(row):
            entry["keys"].add(m)
        for m in PHONE_RE.findall(row):
            entry["keys"] |= subject_keys(m)

    for row in rows:
        for rid in set(RESOURCE_RE.findall(row)):
            note(rid, row)
        for alias, rid in aliases.items():
            if alias and alias in row and rid not in RESOURCE_RE.findall(row):
                note(rid, row)

    # Detail panes put the selected resource id in a heading row and its
    # fields on the following rows; carry identifiers from the header block.
    header_ids = [rid for rid in found if any(i < 6 for i, r in enumerate(rows) if rid in r)]
    head_keys: set[str] = set()
    for r in rows[:8]:
        for m in EMAIL_RE.findall(r):
            head_keys |= subject_keys(m)
        for m in USER_REF_RE.findall(r):
            head_keys.add(m)

    out = []
    for rid, entry in found.items():
        kind = resource_kind(rid)
        keys = set(entry["keys"])
        if rid in header_ids:
            keys |= head_keys
        joined = " ".join(entry["rows"]).lower()
        state: dict = {"presence": "PRESENT", "keys": keys, "exposed": {k for k in keys if not k.startswith("user_")}}
        if kind == "data_flow":
            state["flow"] = "ACTIVE"
            for word, (_, val) in STATE_WORDS.items():
                if word in joined:
                    state["flow"] = val
            if "healthy" in lowered or "continuous" in joined:
                state["flow"] = state["flow"] if any(w in joined for w in STATE_WORDS) else "ACTIVE"
        elif kind == "backup":
            blocked = any(w in lowered for w in ("restore blocked", "restore disabled", "deletion block", "legal hold only"))
            state["restore"] = "BLOCKED" if blocked else "ACTIVE"
        elif kind in ("analytics", "support", "account", "marketing", "tombstone", "search", "export", "object"):
            if "[redacted]" in joined or "retained reference" in lowered and not state["exposed"]:
                state["exposure"] = "CLEAN"
            else:
                state["exposure"] = "IDENTIFIABLE" if (state["exposed"] or kind in ("account", "marketing")) else "CLEAN"
        if kind in ("support", "object") and (".pdf" in joined or "attachment" in lowered):
            state["category"] = "SUPPORT_ATTACHMENT"
        obs = make_obs(meta, meta.captured_at, rid, state, f"media:{meta.kind}", Path(meta.path).name,
                       {"rows": entry["rows"][:6]})
        if obs:
            out.append(obs)
    return out
