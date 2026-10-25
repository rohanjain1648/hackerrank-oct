"""Plain-text records such as shift hand-over notes.

Rows look like ``- <time> | <system> | <resource> | <CATEGORY> | <STATE> | <identifier>``.
"""

from __future__ import annotations

import re

from ..textutil import parse_time
from .common import FileMeta, make_obs
from .records import interpret_lookup

ROW = re.compile(r"^\s*-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*$")


def parse_text(meta: FileMeta, path) -> list:
    out = []
    text = open(path, encoding="utf-8").read()
    meta.text = text
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        t, system, rid, category, result, subject = m.groups()
        state = interpret_lookup(category, result, subject)
        obs = make_obs(meta, parse_time(t), rid, state, "text:handover", "handover",
                       {"line": line.strip()}, system=system)
        if obs:
            out.append(obs)
    return out
