#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only rehearsal of overnight historic reservation reconciliation.

Uses SQLite's online backup API to create a throwaway destination; writes
neither the original classification DB nor historical ledgers / campaign JSON.
Does not issue Search or JEV calls. Run before any live night campaign.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as pipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "pipeline_config.json"))
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    original = pipeline.source_paths(cfg)["classification_db"]
    if not original.is_file():
        raise RuntimeError(f"PRODUCTION_SQLITE_MISSING:{original}")
    with tempfile.TemporaryDirectory(prefix="night_history_preflight_") as d:
        isolated = Path(d) / "rehearsal.sqlite"
        # Back up a consistent SQLite snapshot, including committed WAL data.
        # Open production read-only, never write or checkpoint its ledger.
        with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(isolated)) as dst:
                src.backup(dst)
        metrics = pipeline.import_prior_reservations(isolated, pipeline.OUT)
        with closing(sqlite3.connect(isolated)) as db:
            n = db.execute(
                "SELECT COUNT(*) FROM collection_attempt_reservations"
            ).fetchone()[0]
        print(json.dumps({
            "status": "PASS",
            "dry_run": True,
            "api_calls": 0,
            "official_database_modified": False,
            "temporary_ledger_reservations": n,
            **metrics,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED", "api_calls": 0,
            "official_database_modified": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }, indent=2, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
