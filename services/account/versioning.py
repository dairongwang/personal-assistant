"""Semantic version helpers shared by release manifests and client gating."""

from __future__ import annotations

import re

_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([0-9A-Za-z][0-9A-Za-z.-]*))?\s*$")


def parse_version(text: str) -> tuple:
    m = _RE.match(text or "")
    if not m:
        return (-1, -1, -1, 0, ())
    major, minor, patch, pre = m.groups()
    parts: list = []
    if pre:
        for p in re.split(r"[.-]", pre):
            parts.append((0, int(p)) if p.isdigit() else (1, p.lower()))
    return (int(major), int(minor or 0), int(patch or 0), 0 if pre else 1, tuple(parts))


def compare(a: str, b: str) -> int:
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def is_valid(text: str) -> bool:
    return parse_version(text)[0] >= 0
