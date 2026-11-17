#!/usr/bin/env python3
"""Pilot OpenSERP full, strictly isolated: artifact -> collect -> cache -> classify -> audit.

Requires preexisting openserp_staging.json. No live Search is requested.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from search_provider_extensions import load_openserp_staging


def _digest(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def prepare_pilot(root: Path, staging: Path, max_api_calls: int, max_tokens: int):
    import pandas as pd

    config_path = root / "pipeline_config.json"
    rule_path = root / "regras_negocio.json"
    policy_path = root / "collection_quality_policy.json"
    for path in (config_path, rule_path, policy_path, staging):
        if not path.is_file():
            raise FileNotFoundError(f"PILOT_REQUIRED_SOURCE_MISSING: {path}")
    doc = json.loads(staging.read_text(encoding="utf-8"))
    cohorts = doc.get("cohorts")
    if doc.get("schema") != "openserp-staging-v1" or not isinstance(cohorts, list) or not cohorts:
        raise ValueError("PILOT_STAGING_INVALID")
    if not 1 <= len(cohorts) <= 20:
        raise ValueError("PILOT_COHORT_LIMIT: 1..20")
    if not 1 <= max_api_calls <= 100 or not 1 <= max_tokens <= 200000:
        raise ValueError("PILOT_BUDGET_INVALID")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    from pipeline_produtos import derive_query_model
    records = []
    for cohort in cohorts:
        brand, model = (str(cohort.get(k) or "").strip() for k in ("brand", "model"))
        if not brand or not model:
            raise ValueError("PILOT_EMPTY_SKU")
        query_template = config.get("collection", {}).get("query_template", "{brand} {search_model}")
        actual_query = query_template.format(
            brand=brand, model=model, search_model=derive_query_model(brand, model, config)
        ).strip()
        # Guarantee the exact search that the production pipeline will request.
        load_openserp_staging(str(staging), brand, model, actual_query)
        records.append({"Marca": brand, "Modelo": model})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parent = root / "piloto_argentina" / "openserp_full"
    run = parent / (stamp + "_" + hashlib.sha256(staging.read_bytes()).hexdigest()[:8])
    run.mkdir(parents=True, exist_ok=False)

    input_file = run / "pilot_input.xlsx"
    pd.DataFrame(records).to_excel(input_file, index=False)
    # Completely empty isolated baselines; never copy approved official values.
    columns = ["Marca", "Modelo", "O que é (Tipo)", "Alimentação",
               "Category General", "FAMILY", "Título Real", "Status",
               "Próxima Ação", "Revisão", "Revisão B.I"]
    master = run / "pilot_master.xlsx"
    final = run / "pilot_final.xlsx"
    pd.DataFrame(columns=columns).to_excel(master, index=False, sheet_name="Consolidado")
    pd.DataFrame(columns=columns).to_excel(final, index=False, sheet_name="Base Final")

    cfg = json.loads(json.dumps(config))
    cfg["files"].update({
        "input": str(input_file),
        "cache": str(run / "pilot_cache.sqlite"),
        "classification_db": str(run / "pilot_classification.sqlite"),
        "master": str(master),
        "final": str(final),
        "output": str(run / "pilot_classification.xlsx"),
        "inventory_output": str(run / "pilot_inventory.xlsx"),
        "precollection_report": str(run / "pilot_precollection.xlsx"),
        "collection_report": str(run / "pilot_collection.xlsx"),
        "pending_with_cache": str(run / "pilot_pending_with_cache.xlsx"),
        "pending_without_cache": str(run / "pilot_pending_without_cache.xlsx"),
        "baseline_backup_dir": str(run / "Backups"),
        "baseline_audit": str(run / "audit.csv"),
        "collection_report_cache_rebuild": str(run / "pilot_curation.xlsx"),
        "evidence_export_jsonl": str(run / "pilot_evidence.jsonl"),
        "collection_policy": str(policy_path),
    })
    cfg["collection"]["provider"] = "openserp"
    cfg["collection"]["providers"]["openserp"]["artifact_path"] = str(staging)
    cfg_file = run / "isolated_config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    official_files = []
    for name in ("input", "cache", "classification_db", "master", "final"):
        original = Path(str(config.get("files", {}).get(name) or ""))
        if not original.is_absolute():
            original = root / original
        if original.exists() and original.is_file():
            official_files.append({"name": name, "path": str(original),
                                   "sha256": _digest(original)})
    manifest = {
        "provider": "openserp", "execution_mode": "ISOLATED_STAGED_ARTIFACT",
        "network_search_authorized": False,
        "auto_promotion": False, "cohorts": records,
        "llm_max_calls": max_api_calls, "llm_max_tokens": max_tokens,
        "sources": official_files, "status": "PREPARED",
        "run": str(run),
    }
    (run / "pilot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return run, cfg_file, manifest


def main():
    ap = argparse.ArgumentParser(description="Piloto completo OpenSERP isolado")
    ap.add_argument("--artifact", default="openserp_staging.json")
    ap.add_argument("--llm-provider", choices=("jev", "clef", "local"), default="jev")
    ap.add_argument("--max-api-calls", type=int, default=15)
    ap.add_argument("--max-tokens", type=int, default=30000)
    ap.add_argument("--prepare-only", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent
    artifact = Path(args.artifact).expanduser().resolve()
    run, cfg, report = prepare_pilot(root, artifact, args.max_api_calls, args.max_tokens)
    command = [
        sys.executable, str(root / "pipeline_produtos.py"), "--mode", "full",
        "--config", str(cfg), "--rules", str(root / "regras_negocio.json"),
        "--collection-policy", str(root / "collection_quality_policy.json"),
        "--search-provider", "openserp", "--openserp-artifact", str(artifact),
        "--input", str(run / "pilot_input.xlsx"),
        "--cache", str(run / "pilot_cache.sqlite"),
        "--master", str(run / "pilot_master.xlsx"),
        "--final", str(run / "pilot_final.xlsx"),
        "--db", str(run / "pilot_classification.sqlite"),
        "--output", str(run / "pilot_classification.xlsx"),
        "--inventory-output", str(run / "pilot_inventory.xlsx"),
        "--max-search-new", str(len(report["cohorts"])),
        "--max-items", str(len(report["cohorts"])),
        "--llm-provider", args.llm_provider,
        "--max-api-calls", str(args.max_api_calls),
        "--max-tokens", str(args.max_tokens), "--yes",
    ]
    print(f"ISOLATED_RUN={run}", flush=True)
    print(f"OPEN_SERP_SEARCH_CALLS=0 | ARTIFACT_SKUS={len(report['cohorts'])}", flush=True)
    if args.prepare_only:
        print("PREPARED_ONLY | NO_APIS_USED | CACHE_WRITES=0", flush=True)
        return 0
    result_code = 2
    try:
        with (run / "pipeline_full.log").open("w", encoding="utf-8") as log:
            result_code = subprocess.call(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    finally:
        changes = []
        for origin in report["sources"]:
            p = Path(origin["path"])
            if not p.is_file() or _digest(p) != origin["sha256"]:
                changes.append(origin["name"])
        report["original_files_modified"] = changes
        report["pipeline_exit_code"] = result_code
        report["status"] = "FAILED" if result_code else "PIPELINE_FINISHED_NEEDS_REVIEW"
        try:
            from contextlib import closing
            from pipeline_produtos import TavilyReadOnlyStore
            with closing(TavilyReadOnlyStore(run / "pilot_cache.sqlite")) as store:
                report["raw_states"] = [
                    {"brand": x["Marca"], "model": x["Modelo"],
                     "raw_state": store.get(x["Marca"], x["Modelo"]).state}
                    for x in report["cohorts"]
                ]
        except Exception as exc:
            report["raw_audit_error"] = f"{type(exc).__name__}: {exc}"
        (run / "pilot_manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if changes:
            raise RuntimeError(f"OFFICIAL_FILES_MODIFIED: {changes}")
    print(f"PIPELINE_EXIT={result_code} REPORT={run / 'pilot_manifest.json'}", flush=True)
    return 0 if result_code == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
