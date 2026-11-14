#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""50 new SKU real FULL pilot. Offline prepare -> explicit paid execute -> audit.

The pilot uses the SAME official baseline and SQLite that production uses.
No production rollout is initiated by this script, even after success.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p


def checksum(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as fd:
        for chunk in iter(lambda: fd.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def sqlite_backup(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as src:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("PILOT_SQLITE_CORRUPT:" + str(source))
        with closing(sqlite3.connect(destination)) as dest:
            src.backup(dest)


def config_paths(config):
    root = Path(config).resolve().parent
    cfg = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    names = ("input", "master", "final", "cache", "classification_db", "output",
             "collection_report")
    return cfg, {name: (root / cfg["files"][name]).resolve() for name in names}


def attempted_keys(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "collection_attempt_reservations" not in tables:
            return set()
        return {x[0] for x in db.execute("SELECT business_key FROM collection_attempt_reservations")}


def get_fresh(source, paths, count):
    rows = p.read_input_rows(source)
    eligible = [(p.safe(r.brand_original), p.safe(r.model_original),
                 p.business_key(r.brand_original, r.model_original)) for r in rows if r.valid]
    if len(eligible) < 15000:
        raise RuntimeError(f"PILOT_SOURCE_TOO_SMALL: valid={len(eligible)}; expected >=15000 (19K source)")
    cached = set()
    with closing(p.TavilyReadOnlyStore(paths["cache"])) as store:
        cached = {key for _, _, key in store.catalog()}
    locked = p.load_baseline_inventory(paths["master"], paths["final"])["processed"]
    attempts = attempted_keys(paths["classification_db"])
    seen = set()
    fresh = []
    for brand, model, key in eligible:
        if key in seen:
            continue
        seen.add(key)
        if key in cached or key in attempts or key in locked:
            continue
        fresh.append((brand, model, key))
    if len(fresh) < count:
        raise RuntimeError(f"PILOT_FRESH_COHORT_INSUFFICIENT: {len(fresh)}<{count}")

    # Deterministic, brand-diversified sample; do not cherry-pick easy products.
    by_brand = defaultdict(list)
    for row in fresh:
        by_brand[row[0].casefold()].append(row)
    for group in by_brand.values():
        group.sort(key=lambda r: hashlib.sha256(("pilot-50-v1|" + r[2]).encode()).hexdigest())
    order = sorted(by_brand, key=lambda brand: hashlib.sha256(("brand|" + brand).encode()).hexdigest())
    selected = []
    while len(selected) < count:
        progress = False
        for brand in order:
            if by_brand[brand]:
                selected.append(by_brand[brand].pop(0))
                progress = True
                if len(selected) == count:
                    break
        if not progress:
            break
    if len(selected) != count:
        raise RuntimeError("PILOT_SELECTION_INTERNAL_ERROR")
    return selected, {"source_rows":len(rows),"valid_rows":len(eligible),
                      "unique_source_keys":len(seen),
                      "eligible_new_keys":len(fresh),
                      "represented_brands":len({r[0].casefold() for r in selected})}


def prepare(args):
    import pandas as pd
    config = Path(args.config).resolve()
    cfg, paths = config_paths(config)
    source = Path(args.source).resolve()
    if not source.is_file():
        raise RuntimeError("PILOT_SOURCE_FILE_MISSING:" + str(source))
    for key in ("master", "final", "cache", "classification_db"):
        if not paths[key].is_file():
            raise RuntimeError("PILOT_OFFICIAL_FILE_MISSING:" + str(paths[key]))
    selected, metrics = get_fresh(source, paths, args.count)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = config.parent / "pilotos" / ("stable_50_" + stamp)
    folder.mkdir(parents=True, exist_ok=False)
    pilot_input = folder / "pilot_50_input.xlsx"
    pd.DataFrame([{"Marca":x[0], "Modelo":x[1]} for x in selected]).to_excel(
        pilot_input, index=False, engine="openpyxl")
    # The frozen selected SKU set is the only input authorized for the pilot.
    manifest = {
        "state": "PREPARED_OFFLINE", "source":str(source),
        "source_sha256":checksum(source),
        "config":str(config), "config_sha256":checksum(config),
        "pilot_input":str(pilot_input), "pilot_input_sha256":checksum(pilot_input),
        "count":args.count,"selected":[{"brand":b,"model":m,"key":k} for b,m,k in selected],
        "baseline_sha256": {name:checksum(paths[name])
                            for name in ("master","final","cache","classification_db")},
        "readiness":metrics, "prepared_at":datetime.now().isoformat(),
        "max_search":args.count, "max_llm_calls":args.max_calls,
        "max_tokens":args.max_tokens,
        "search_calls":0, "llm_calls":0, "official_writes":False,
    }
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({
        "status":"PILOT_50_PREPARED_NO_APIS",
        "manifest":str(manifest_path),
        "input_50":str(pilot_input),
        "fresh":metrics,
        "limits":{"Search":args.count,"JEV HTTP":args.max_calls,
                  "JEV tokens":args.max_tokens},
        "next_command":f'& "C:\\p\\venv\\Scripts\\python.exe" .\\scripts\\pilot_50_full_gate.py execute --manifest "{manifest_path}"',
    },ensure_ascii=False,indent=2))
    return 0


def inspect_final(paths, selected, before_accept, before_bi, result_file):
    import pandas as pd
    from tools.final_output_gate import validate
    input_50 = selected["pilot_input"]
    failures, input_rows, output_rows = validate(input_50, paths["final"])
    if failures or input_rows != 50:
        raise RuntimeError("PILOT_FINAL_COVERAGE_FAILED:" + str(failures[:8]))
    after = p.load_baseline_inventory(paths["master"], paths["final"])
    lost = before_accept - after["processed"]
    if lost:
        raise RuntimeError("PILOT_PRIOR_ACCEPTED_LOST:" + str(len(lost)))
    p._assert_bi_lock_snapshot(p._read_business_sheet(paths["final"])[0], before_bi,
                               "pilot 50 after full")
    frame, _ = p._read_business_sheet(paths["final"])
    final_lookup = defaultdict(list)
    for _, row in frame.iterrows():
        final_lookup[(p.safe(row.get("Marca")), p.safe(row.get("Modelo")))].append(row)
    records = pd.read_excel(result_file, sheet_name="Resultados",
                            dtype=object,keep_default_na=False).to_dict("records")
    wanted = {(r["brand"], r["model"]) for r in selected["selected"]}
    if len(records) > 50 or any((p.safe(r.get("Marca")), p.safe(r.get("Modelo"))) not in wanted
                                 for r in records):
        raise RuntimeError("PILOT_RESULT_OUTSIDE_50")
    source_titles = 0
    missing_title = []
    cache_states = Counter()
    with closing(p.TavilyReadOnlyStore(paths["cache"])) as store:
        for pair in wanted:
            raw = store.get(*pair)
            cache_states[raw.state] += 1
            if raw.state not in {"RAW_OK","RAW_OK_MERGED","RAW_EMPTY"}:
                raise RuntimeError("PILOT_MISSING_SEARCH_ATTEMPT:" + repr(pair))
            if raw.state in {"RAW_OK","RAW_OK_MERGED"}:
                title = p.safe(p._manual_review_bundle(raw).get("Título Referência"))
                if title:
                    source_titles += 1
                    if not any(p.safe(row.get("Título Real")) for row in final_lookup.get(pair,[])):
                        missing_title.append(pair)
    if missing_title:
        raise RuntimeError("PILOT_SEARCH_TITLE_NOT_VISIBLE:" + repr(missing_title[:8]))

    accepted = completed = partial = 0
    mismatches = []
    for rec in records:
        pair = (p.safe(rec.get("Marca")), p.safe(rec.get("Modelo")))
        state = p.safe(rec.get("Resolution State")).upper()
        if state in {"RESOLVED", "CLASSIFIED_CLEF"} and all(
                p.safe(rec.get(c)) for c in ("O que é (Tipo)", "Category General", "FAMILY")):
            completed += 1
            if not any(all(p.safe(row.get(c)) == p.safe(rec.get(c))
                           for c in ("O que é (Tipo)", "Category General", "FAMILY"))
                       for row in final_lookup[pair]):
                mismatches.append(pair)
        else:
            partial += 1
        if p._record_is_accepted(rec):
            accepted += 1
            if p.business_key(*pair) not in after["final_accepted"]:
                mismatches.append(pair)
        elif state == "CLASSIFIED_CLEF" and p.business_key(*pair) in after["final_accepted"]:
            mismatches.append(pair)
    if mismatches:
        raise RuntimeError("PILOT_RESULT_BASELINE_CONFLICT:" + repr(mismatches[:8]))
    return {"final_rows":output_rows,"search_cache_states":dict(cache_states),
            "complete_classifications":completed,"auto_accepted":accepted,
            "review_or_partial_results":partial,
            "search_titles_found":source_titles,
            "search_titles_missing_in_final":len(missing_title),
            "result_rows":len(records),
            "prior_accepted_preserved":len(before_accept),
            "bi_locks_preserved":len(before_bi),
            "complete_rate_of_fifty":round(completed/50,3)}


def execute(args):
    manifest_path = Path(args.manifest).resolve()
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    if m.get("state") != "PREPARED_OFFLINE" or m.get("count") != 50:
        raise RuntimeError("PILOT_MANIFEST_NOT_PREPARED_FOR_50")
    config = Path(m["config"])
    cfg, paths = config_paths(config)
    pilot_input = Path(m["pilot_input"])
    if checksum(config) != m["config_sha256"]:
        raise RuntimeError("PILOT_CONFIG_CHANGED_AFTER_PREPARE")
    if checksum(pilot_input) != m["pilot_input_sha256"]:
        raise RuntimeError("PILOT_FROZEN_INPUT_CHANGED")
    if checksum(Path(m["source"])) != m["source_sha256"]:
        raise RuntimeError("PILOT_19K_SOURCE_CHANGED")
    for name, expected in m["baseline_sha256"].items():
        if checksum(paths[name]) != expected:
            raise RuntimeError("PILOT_BASELINE_CHANGED_SINCE_PREPARE:" + name)
    attempted = attempted_keys(paths["classification_db"])
    with closing(p.TavilyReadOnlyStore(paths["cache"])) as cache:
        existing = {key for _,_,key in cache.catalog()}
    blocked = p.load_baseline_inventory(paths["master"],paths["final"])["processed"]
    keys = {row["key"] for row in m["selected"]}
    if len(keys) != 50 or keys & (attempted | existing | blocked):
        raise RuntimeError("PILOT_50_NOT_ALL_FRESH_ANYMORE")
    if args.confirm != "EXECUTAR_50":
        raise RuntimeError("PILOT_EXPLICIT_CONFIRMATION_REQUIRED: --confirm EXECUTAR_50")

    # Close Excel first: prevent spending API money before discovering a lock.
    for key in ("master","final"):
        with paths[key].open("r+b"):
            pass
    start_accepted = p.load_baseline_inventory(paths["master"],paths["final"])["processed"]
    initial_bi = p._load_bi_lock_snapshot(paths["final"])
    backup_dir = manifest_path.parent / "before_live_backup"
    backup_dir.mkdir(exist_ok=False)
    for key in ("master","final"):
        shutil.copy2(paths[key],backup_dir / paths[key].name)
    for key in ("cache","classification_db"):
        sqlite_backup(paths[key],backup_dir / paths[key].name)

    command = [
        sys.executable, str(ROOT / "pipeline_produtos.py"),
        "--mode","full","--input",str(pilot_input),
        "--search-provider","tavily","--llm-provider","jev",
        "--max-search-new","50","--max-items","50",
        "--max-api-calls",str(m["max_llm_calls"]),
        "--max-tokens",str(m["max_tokens"]),
        "--yes",
    ]
    report_path = manifest_path.parent / "pilot_50_result.json"
    log_path = manifest_path.parent / "pilot_50_live.log"
    m["state"] = "LIVE_STARTED"
    m["live_started_at"] = datetime.now().isoformat()
    m["command"] = command[1:]
    manifest_path.write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding="utf-8")
    exit_code = -999
    try:
        with log_path.open("w",encoding="utf-8") as output:
            proc = subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                                    errors="replace",bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                output.write(line)
                output.flush()
            exit_code = proc.wait()
        report = {"exit_code":exit_code,"log":str(log_path),
                  "backup":str(backup_dir),"status":"BLOCKED"}
        if exit_code != 0:
            report["reason"] = "PIPELINE_FULL_RETURNED_NONZERO_NO_AUTO_RETRY"
        else:
            report["audited"] = inspect_final(paths,m,start_accepted,initial_bi,paths["output"])
            # Stop even after technically passing: QA of title/type/taxonomy
            # against real sources is mandatory before the 19K rollout.
            if report["audited"]["complete_classifications"] < 35:
                report["reason"] = "PILOT_CLASSIFICATION_YIELD_BELOW_35_OF_50"
            else:
                report["status"] = "TECHNICAL_PASS_AWAITING_QUALITY_REVIEW"
                report["reason"] = "REVIEW_10_REAL_SKUS_BEFORE_19K"
        report["finished_at"] = datetime.now().isoformat()
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))
        m["state"] = report["status"]
        manifest_path.write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding="utf-8")
        return 0 if report["status"]=="TECHNICAL_PASS_AWAITING_QUALITY_REVIEW" else 2
    except BaseException as error:
        report_path.write_text(json.dumps({"status":"BLOCKED","reason":repr(error),
            "exit_code":exit_code,"backup":str(backup_dir),"log":str(log_path)},
            ensure_ascii=False,indent=2),encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action",required=True)
    setup = subs.add_parser("prepare")
    setup.add_argument("--config",default=str(ROOT / "pipeline_config.json"))
    setup.add_argument("--source",required=True,help="Actual 19K Excel, not the 1030-SKU input")
    setup.add_argument("--count",type=int,default=50,choices=[50])
    setup.add_argument("--max-calls",type=int,default=65)
    setup.add_argument("--max-tokens",type=int,default=350000)
    live = subs.add_parser("execute")
    live.add_argument("--manifest",required=True)
    live.add_argument("--confirm",default="")
    args = parser.parse_args()
    if args.action=="prepare":
        if args.max_calls<50 or args.max_tokens<100000:
            parser.error("API caps insufficient for meaningful 50-SKU pilot")
        return prepare(args)
    return execute(args)


if __name__=="__main__":
    raise SystemExit(main())
