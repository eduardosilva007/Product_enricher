#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed, content-addressed cache for derived baseline key inventories.

No cached classification proposals, evidence, human edits, or workbook objects.
The caller always hashes the *current* source file and code version. A cache
entry contains only derived key sets and is never treated as the source of truth.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Callable, Iterable, Tuple

LOG = logging.getLogger(__name__)
SCHEMA = "baseline_keys_v1"
_FIELDS = ("accepted", "manual", "bi_locked")
_CACHE_DIR = Path(__file__).resolve().parent / ".runtime_cache" / "baseline_keys"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _entry_path(path: Path) -> Path:
    name_hash = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    return _CACHE_DIR / (name_hash + ".json")


def cached_baseline_keys(
    path: Path,
    loader: Callable[[Path], Tuple[set, set, set]],
    *,
    logic_digest: str,
) -> Tuple[set, set, set]:
    """Return independent copies of derived sets; transparently recompute on miss.

    A full content hash prevents stale BI/manual locks if an Excel file changes
    without a detectable size/timestamp change. Never cache failed/incomplete
    reads. A concurrent edit between two digests raises rather than accepting
    a potentially inconsistent baseline.
    """
    if not path.is_file():
        return loader(path)
    t0 = time.perf_counter()
    source_sha = _digest(path)
    entry = _entry_path(path)
    try:
        cached = json.loads(entry.read_text(encoding="utf-8"))
        if (
            cached.get("schema") == SCHEMA
            and cached.get("source_sha256") == source_sha
            and cached.get("logic_sha256") == logic_digest
            and all(isinstance(cached.get(k), list) for k in _FIELDS)
        ):
            values = tuple(set(cached[k]) for k in _FIELDS)
            # The serialized union is accepted as a cache only if its lists
            # contain unique string keys. Otherwise recompute from Excel.
            if all(
                len(values[i]) == len(cached[k])
                and all(isinstance(v, str) for v in cached[k])
                for i, k in enumerate(_FIELDS)
            ) and values[1].issubset(values[0]) and values[2].issubset(values[0]):
                if _digest(path) != source_sha:
                    raise RuntimeError("BASELINE_CHANGED_DURING_CACHE_READ")
                LOG.info(
                    "BASELINE INVENTORY CACHE_HIT | file=%s | elapsed=%.2fs",
                    path.name, time.perf_counter() - t0,
                )
                return values
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    # Filesystem errors affecting the cache are nonfatal; the original Excel
    # is still read and retains full authority.
    values = loader(path)
    if _digest(path) != source_sha:
        raise RuntimeError("BASELINE_CHANGED_DURING_CACHE_SCAN")
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": SCHEMA,
            "source_sha256": source_sha,
            "logic_sha256": logic_digest,
            **{k: sorted(values[i]) for i, k in enumerate(_FIELDS)},
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=entry.parent,
            prefix=entry.stem + ".", suffix=".tmp", delete=False,
        ) as fh:
            tmp = Path(fh.name)
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, entry)
    except (OSError, TypeError, ValueError):
        LOG.warning("BASELINE INVENTORY CACHE_WRITE_SKIPPED | file=%s", path.name)
        if "tmp" in locals():
            tmp.unlink(missing_ok=True)
    LOG.info(
        "BASELINE INVENTORY CACHE_MISS | file=%s | elapsed=%.2fs",
        path.name, time.perf_counter() - t0,
    )
    return values
