"""Filesystem locations, resolved relative to the repository root."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = Path(os.environ.get("AUDIT_DATASET_DIR", REPO_ROOT / "dataset"))
OUTPUT_CSV = REPO_ROOT / "output.csv"
# Caches (OCR text, LLM answers, run traces) live outside code/ so they are
# never shipped inside code.zip.
CACHE_DIR = Path(os.environ.get("AUDIT_CACHE_DIR", REPO_ROOT / ".cache"))
RUNS_DIR = CACHE_DIR / "runs"


def dataset_file(name: str) -> Path:
    return DATASET_DIR / name
