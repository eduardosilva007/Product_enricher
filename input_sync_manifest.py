#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative, content-addressed record of successful baseline synchronizations.

Missing/corrupt manifests ALWAYS cause full synchronization. A manifest is
published only after a completed, verified export; it is invalidated by any
byte change in input/master/final/cache/configuration/rules.
"""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

SCHEMA = "input_baseline_sync_v1"
MARKER = Path(__file__).resolve().parent / ".runtime_cache" / "successful_input_sync.json"


def _digest(p):
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for buf in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def snapshot(paths):
    """Include SQLite WAL contents when applicable, not timestamps alone."""
    out = {}
    for name, original in sorted(paths.items()):
        p = Path(original).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"SYNC_SNAPSHOT_MISSING:{name}:{p}")
        out[name] = {"path": str(p), "sha256": _digest(p)}
        if name == "cache":
            wal = Path(str(p) + "-wal")
            if wal.is_file():
                out["cache_wal"] = {"path": str(wal.resolve()), "sha256": _digest(wal)}
    return out


def is_current(paths, marker=MARKER):
    t0 = time.perf_counter()
    try:
        data = json.loads(Path(marker).read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA or data.get("status") != "SYNC_COMPLETED":
            return False
        valid = data.get("files") == snapshot(paths)
        return valid
    except (OSError, ValueError, TypeError):
        return False
    finally:
        import logging
        logging.getLogger(__name__).info(
            "INPUT BASELINE FINGERPRINT | elapsed=%.2fs", time.perf_counter()-t0
        )


def record_success(paths, marker=MARKER):
    before = snapshot(paths)
    dest = Path(marker)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": SCHEMA, "status": "SYNC_COMPLETED", "files": before}
    tmp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8",
                                prefix=dest.stem+".", suffix=".tmp",
                                dir=dest.parent, delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
        if snapshot(paths) != before:
            raise RuntimeError("SYNC_FILES_CHANGED_DURING_FINGERPRINT")
        os.replace(tmp, dest)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
