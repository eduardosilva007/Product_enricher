#!/usr/bin/env python3
"""Read-only forensic scan of canonical and controlled-night Search reservations.

No project pipeline imports, no network, no sqlite writes and no automatic repairs.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3


def read_reservations(path: Path):
    if not path.is_file():
        raise RuntimeError(f"MISSING_DATABASE:{path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_attempt_reservations'").fetchone()
            if not exists:
                return []
            return [dict(key=k, account=a, provider=p) for k,a,p in db.execute(
                "SELECT business_key, account, provider FROM collection_attempt_reservations")]
    except sqlite3.Error as exc:
        raise RuntimeError(f"UNREADABLE_LEDGER:{path}:{type(exc).__name__}") from exc


def scan(config_path: Path, history_root: Path, target_key: str):
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    dbname = cfg["files"]["classification_db"]
    official = (config_path.parent / dbname).resolve()
    indexed = defaultdict(list)
    official_rows = read_reservations(official)
    for row in official_rows:
        indexed[row["key"]].append(dict(source="canonical", account=row["account"], provider=row["provider"]))
    runs = 0
    historical = 0
    anomalies = []
    for report in sorted(history_root.glob("*/run_report.json")):
        state=json.loads(report.read_text(encoding="utf-8"))
        if not state.get("stages",{}).get("collection",{}).get("attempts",0):
            continue
        runs += 1
        conf = json.loads((report.parent / "isolated_config.json").read_text(encoding="utf-8"))
        prior = Path(conf["files"]["classification_db"])
        if not prior.is_absolute():
            prior = (report.parent / prior).resolve()
        rows = read_reservations(prior)
        keys = {row["key"] for row in rows}
        cohort = set(state.get("cohort_keys", []))
        missing = sorted(cohort-keys)
        if missing:
            anomalies.append(dict(run=report.parent.name, kind="UNACCOUNTED_COHORT", count=len(missing), first=missing[:5]))
        for row in rows:
            indexed[row["key"]].append(dict(source=report.parent.name, account=row["account"], provider=row["provider"]))
            historical+=1
    campaigns=[]
    for manifest in sorted((history_root / "campaigns").glob("*/campaign.json")):
        try:
            c=json.loads(manifest.read_text(encoding="utf-8"))
            if c.get("status") in {"BLOCKED", "RUNNING", "READY", "BUDGET_EXHAUSTED"}:
                campaigns.append({"directory":str(manifest.parent),
                    "status":c.get("status"),
                    "reason":c.get("reason"),
                    "pending_run_creation":c.get("pending_run_creation"),
                    "active_run":c.get("active_run"),
                    "audited_products":c.get("audited_products"),
                    "reserved_jev_calls":c.get("reserved_jev_calls"),
                    "reserved_tokens":c.get("reserved_tokens")})
        except (OSError,ValueError,TypeError):
            anomalies.append(dict(run=str(manifest),kind="UNREADABLE_CAMPAIGN_MANIFEST"))
    conflict = {}
    for key, vals in indexed.items():
        variants = {(v["account"], v["provider"]) for v in vals}
        if len(variants)>1:
            conflict[key] = vals
    return {
        "status": "CONFLICTS_FOUND" if conflict or anomalies else "NO_CONFLICTS",
        "canonical_reservations":len(official_rows),
        "historical_runs_scanned":runs,
        "historical_ledger_rows":historical,
        "unique_keys_combined":len(indexed),
        "conflicting_keys":len(conflict),
        "unaccounted_cohorts":anomalies[:10],
        "unfinished_campaigns":campaigns[-10:],
        "requested_key":target_key,
        "requested_key_records":indexed.get(target_key,[]),
        "conflict_samples": [{"key":k,"records":v} for k,v in list(sorted(conflict.items()))[:20]],
        "api_calls":0,
        "read_only":True,
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="pipeline_config.json")
    parser.add_argument("--history", default="piloto_argentina/controlled_night")
    parser.add_argument("--key", default="dca|ajet15")
    a=parser.parse_args()
    print(json.dumps(scan(Path(a.config).resolve(),Path(a.history).resolve(),a.key),ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
