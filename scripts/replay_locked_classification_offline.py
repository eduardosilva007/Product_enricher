#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reapply an Excel-locked classification XLSX, offline, with staging and rollback.

Default is DRY-RUN: official Excel/SQLite are not changed. --commit requires a
successful staged export, exact result identity, approval parity and human locks.
Does NOT run Search, LLM, embedding or reclassification.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_failed_sqlite_run(db_path, result_path, metrics, records, rules):
    """Fail-closed provenance for a lost/blank Excel failure marker.

    The Excel may have been copied after a locked run. Require the exact
    ordered cohort hash, the matching FAILED run from SQLite, no intervening
    classification, a bounded file timestamp, and many exact checkpoints.
    Never infer recovery eligibility merely from identical row counts.
    """
    from datetime import datetime, timezone, timedelta
    path = Path(db_path)
    if not path.is_file():
        raise RuntimeError("SQLITE_LOCKED_RUN_PROVENANCE_DB_MISSING")
    keys = [p.business_key(p.safe(x.get("Marca")), p.safe(x.get("Modelo")))
            for x in records]
    fingerprint = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:12]
    if p.safe(metrics.get("Pilot Cohort Hash")) != fingerprint:
        raise RuntimeError("SQLITE_LOCKED_RUN_COHORT_HASH_MISMATCH")
    file_time = datetime.fromtimestamp(Path(result_path).stat().st_mtime,
                                       tz=timezone.utc)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLITE_LOCKED_RUN_DB_CORRUPT")
        rows = db.execute(
            "SELECT run_id,started_at,finished_at,status,mode,pipeline_version,"
            "rules_hash,note FROM runs WHERE mode IN ('classify','full','recover') "
            "ORDER BY started_at DESC LIMIT 30").fetchall()
        # The report is a snapshot of the LAST run that wrote it, not
        # necessarily of the older failed run. A later completed run can
        # overwrite Classificacao_v3.xlsx while keeping the same 338 rows.
        # Pair the actual file timestamp with the nearest matching SQLite run.
        contemporary = []
        for entry in rows:
            rid, started, finished, status, mode, version, rhash, note = entry
            # FULL and recovery runs also overwrite Classificacao_v3.xlsx.
            # Never associate such a file with an older CLASSIFY failure.
            if mode not in {"classify", "full", "recover"} or not finished:
                continue
            try:
                started_at = datetime.fromisoformat(started).astimezone(timezone.utc)
                finished_at = datetime.fromisoformat(finished).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
            if started_at <= file_time <= finished_at + timedelta(minutes=30):
                contemporary.append((rid, started_at, finished_at, status, version,
                                     rhash, note))
        if not contemporary:
            raise RuntimeError("SQLITE_RESULT_PROVENANCE_NOT_FOUND: no contemporaneous classify run")
        contemporary.sort(key=lambda item: (item[1], item[2]), reverse=True)
        rid, started_at, finished_at, source_status, version, rhash, note = contemporary[0]
        # IMPORTANT: Do not fall back to an older Excel-lock incident if a
        # newer run wrote the same output pathname. Validate the LATEST run.
        if version != p.PIPELINE_VERSION or rhash != rules.hash:
            raise RuntimeError("SQLITE_RESULT_LATEST_RUN_RULES_OR_VERSION_CHANGED")
        if source_status == "COMPLETED":
            try:
                saved_summary = json.loads(note or "{}")
                same_cohort = (
                    p.safe(saved_summary.get("Pilot Cohort Hash")) == fingerprint
                    and int(saved_summary.get("Elegíveis", -1)) == len(records)
                )
            except (TypeError, ValueError):
                same_cohort = False
            if not same_cohort:
                raise RuntimeError("SQLITE_RESULT_LATEST_COMPLETED_RUN_COHORT_MISMATCH")
        elif source_status == "FAILED" and (
                "BASELINE_PERSISTENCE_FAILED" in (note or "")
                or "BASELINE_FILE_LOCKED" in (note or "")):
            pass
        else:
            raise RuntimeError(
                f"SQLITE_RESULT_LATEST_RUN_NOT_REPLAYABLE: status={source_status}"
            )
        selected = (rid, started_at, finished_at, source_status)
        # Confirm that the workbook actually agrees with saved SQLite
        # classification checkpoints. The earlier run recorded 219 hits;
        # require substantive per-SKU overlap rather than trusting a timestamp.
        known = {}
        for brand, model, body in db.execute(
            "SELECT brand,model,result_json FROM classification_results "
            "WHERE rules_hash=? AND pipeline_version=?",
            (rules.hash, p.PIPELINE_VERSION),
        ):
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                known.setdefault((p.safe(brand), p.safe(model)), []).append(parsed)
        checked = 0
        conflicted = []
        core = ("Resolution State", "O que é (Tipo)",
                "Category General", "FAMILY")
        for record in records:
            # Historical completed checkpoints must NOT invalidate a more
            # recent unresolved/review decision for the same business key.
            if p.safe(record.get("Resolution State")).upper() not in {
                    "RESOLVED", "CLASSIFIED_CLEF"}:
                continue
            pair = (p.safe(record.get("Marca")), p.safe(record.get("Modelo")))
            matches_at_pair = known.get(pair, ())
            if not matches_at_pair:
                continue
            if any(all(p.safe(saved.get(col)) == p.safe(record.get(col))
                       for col in core) for saved in matches_at_pair):
                checked += 1
            else:
                conflicted.append(pair)
        required = min(50, max(1, len(records) // 4))
        if conflicted:
            raise RuntimeError(
                "SQLITE_LOCKED_RUN_CHECKPOINT_CONFLICT:" + repr(conflicted[:5])
            )
        if checked < required:
            raise RuntimeError(
                f"SQLITE_LOCKED_RUN_CHECKPOINT_COVERAGE_LOW:{checked}<{required}"
            )
    return {"source": ("SQLITE_COMPLETED_RUN_AND_CHECKPOINTS"
                       if source_status == "COMPLETED"
                       else "SQLITE_FAILED_BASELINE_AND_CHECKPOINTS"),
            "matched_checkpoints": checked, "cohort_hash": fingerprint,
            "run_id": selected[0], "run_status": source_status,
            "newer_snapshot": source_status == "COMPLETED"}


def read_result(path, count, rules, db_path=None):
    import pandas as pd
    with pd.ExcelFile(path) as xls:
        if not {"Resultados", "Resumo"}.issubset(set(xls.sheet_names)):
            raise RuntimeError("RESULT_SHEETS_MISSING")
        frame = pd.read_excel(xls, sheet_name="Resultados",
                              dtype=object, keep_default_na=False)
        summary = pd.read_excel(xls, sheet_name="Resumo",
                                dtype=object, keep_default_na=False)
    if len(frame) != count:
        raise RuntimeError(f"RESULT_ROW_COUNT:{len(frame)}!={count}")
    if not {"Marca", "Modelo", "Resolution State"}.issubset(frame.columns):
        raise RuntimeError("RESULT_BUSINESS_SCHEMA_INVALID")
    if not {"Métrica", "Valor"}.issubset(summary.columns):
        raise RuntimeError("RESULT_SUMMARY_INVALID")
    # Excel summary labels may be rendered in a legacy console codepage.
    # Use the first two physical columns; do not rely on a Unicode header
    # being typed correctly into a Windows PowerShell here-string.
    if summary.shape[1] < 2:
        raise RuntimeError("RESULT_SUMMARY_ONLY_ONE_COLUMN")
    metrics = {p.safe(row.iloc[0]): row.iloc[1] for _, row in summary.iterrows()}
    if int(metrics.get("cohort_size", -1)) != count:
        raise RuntimeError("RESULT_COHORT_SIZE_MISMATCH")
    if p.safe(metrics.get("Rules Hash")) != rules.hash:
        raise RuntimeError("RESULT_RULES_HASH_CHANGED")
    if p.safe(metrics.get("Pipeline Version")) != p.PIPELINE_VERSION:
        raise RuntimeError("RESULT_PIPELINE_VERSION_CHANGED")
    records = frame.to_dict("records")
    if "BASELINE_FILE_LOCKED" not in p.safe(metrics.get("Baseline Warning")):
        if db_path is None:
            raise RuntimeError("RESULT_NOT_FAILED_BY_EXCEL_LOCK")
        evidence = verify_failed_sqlite_run(db_path, path, metrics, records, rules)
        p.LOG.info(
            "RECOVERY SOURCE VERIFIED IN SQLITE | run=%s status=%s | cohort=%s | "
            "checkpoint_matches=%d | 0 APIs",
            evidence["run_id"], evidence["run_status"], evidence["cohort_hash"],
            evidence["matched_checkpoints"],
        )
    # JEV proposals must remain in DOUBLE_CHECK; stripped public Final cannot
    # infer that safely from a missing or stale action.
    for record in records:
        if p.safe(record.get("Resolution State")).upper() == "CLASSIFIED_CLEF" and p.safe(record.get("Próxima Ação")).upper() not in {
            "DOUBLE_CHECK", "AGUARDAR_REVISAO_PRODUTO_CANONICO",
            "REVISAR_CONFLITO_EQUIVALENCIA",
        }:
            raise RuntimeError("UNSAFE_PROPOSAL_MISSING_DOUBLE_CHECK:" + repr((record.get("Marca"),record.get("Modelo"))))
    keys = set()
    for rec in records:
        brand, model = p.safe(rec.get("Marca")), p.safe(rec.get("Modelo"))
        if not brand or not model:
            raise RuntimeError("RESULT_EMPTY_IDENTITY")
        key = p.business_key(brand, model)
        if key in keys:
            raise RuntimeError("RESULT_DUPLICATE_NORMALIZED_KEY:" + key)
        keys.add(key)
    return records, keys


def business_visibility(records, final_path):
    """Count physical per-SKU classifications already present in Base Final."""
    frame, _ = p._read_business_sheet(Path(final_path))
    lookup = {}
    for _, row in frame.iterrows():
        pair = (p.safe(row.get("Marca")), p.safe(row.get("Modelo")))
        lookup.setdefault(pair, []).append(row)
    visible = missing = incomplete = title_missing = already_present = 0
    fields = ("O que é (Tipo)", "Category General", "FAMILY")
    for rec in records:
        state = p.safe(rec.get("Resolution State")).upper()
        if state not in {"RESOLVED", "CLASSIFIED_CLEF"} or not all(
                p.safe(rec.get(col)) for col in fields):
            incomplete += 1
            continue
        pair = (p.safe(rec.get("Marca")), p.safe(rec.get("Modelo")))
        matches = lookup.get(pair, ())
        if not matches:
            missing += 1
            continue
        if any(all(p.safe(row.get(col)) == p.safe(rec.get(col))
                   for col in fields) for row in matches):
            visible += 1
        else:
            missing += 1
        if p.safe(rec.get("Título Real")) and not any(
                p.safe(row.get("Título Real")) for row in matches):
            title_missing += 1
    return {"complete_results": visible + missing,
            "complete_already_visible": visible,
            "complete_missing_or_different": missing,
            "partial_or_review_results": incomplete,
            "source_titles_missing_in_final": title_missing}


def sqlite_snapshot(src, dest):
    with closing(sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)) as con:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLITE_SOURCE_NOT_HEALTHY:" + str(src))
        with closing(sqlite3.connect(dest)) as output:
            con.backup(output)


def verify(before_master, before_final, staged_master, staged_final, cohort, records, input_path):
    from tools.final_output_gate import validate
    failures, input_rows, final_rows = validate(input_path, staged_final)
    if failures:
        raise RuntimeError("STAGED_PROCV_GATE:" + " | ".join(failures[:8]))

    before = p.load_baseline_inventory(before_master, before_final)
    after = p.load_baseline_inventory(staged_master, staged_final)
    lost = before["processed"] - after["processed"]
    if lost:
        raise RuntimeError("STAGED_PREVIOUS_ACCEPTANCE_LOST:" + str(len(lost)))
    missing_final = after["processed"] - after["final_accepted"]
    if missing_final:
        raise RuntimeError("STAGED_FINAL_ACCEPTANCE_MISSING:" + str(len(missing_final)))
    expected = p._expected_cohort_accepted_after_run(
        cohort, before["final_accepted"], records)
    expected |= p._cohort_equivalence_accepted_keys(staged_final, cohort)
    actual = cohort & after["final_accepted"]
    if actual != expected:
        raise RuntimeError(f"STAGED_ACCEPTED_MISMATCH expected={len(expected)} "
                           f"actual={len(actual)} missing={len(expected-actual)} "
                           f"unexpected={len(actual-expected)}")

    final_frame, _ = p._read_business_sheet(staged_final)
    master_frame, _ = p._read_business_sheet(staged_master)
    p._assert_bi_lock_snapshot(final_frame, p._load_bi_lock_snapshot(before_final),
                               "staged Final")
    p._assert_bi_lock_snapshot(master_frame, p._load_bi_lock_snapshot(before_master),
                               "staged Master")
    original_final, _ = p._read_business_sheet(before_final)
    pairs = lambda frame: Counter((p.safe(row.get("Marca")), p.safe(row.get("Modelo")))
                                  for _, row in frame.iterrows())
    lost_pairs = pairs(original_final) - pairs(final_frame)
    if lost_pairs:
        raise RuntimeError("STAGED_PHYSICAL_ROWS_LOST:" + str(list(lost_pairs.items())[:5]))

    by_exact = {}
    for _, row in final_frame.iterrows():
        by_exact.setdefault((p.safe(row.get("Marca")), p.safe(row.get("Modelo"))), []).append(row)
    completed = 0
    for rec in records:
        if p.safe(rec.get("Resolution State")).upper() not in {"RESOLVED", "CLASSIFIED_CLEF"}:
            continue
        cols = ("O que é (Tipo)", "Category General", "FAMILY")
        if not all(p.safe(rec.get(c)) for c in cols):
            continue
        pair = (p.safe(rec.get("Marca")), p.safe(rec.get("Modelo")))
        if p.business_key(*pair) in before["hard_lock"]:
            continue
        completed += 1
        if not any(all(p.safe(row.get(c)) == p.safe(rec.get(c)) for c in cols)
                   for row in by_exact.get(pair, [])):
            raise RuntimeError("STAGED_CLASSIFICATION_NOT_VISIBLE:" + repr(pair))
    return {"input_rows": input_rows, "final_rows": final_rows,
            "cohort_size": len(cohort), "complete_exact_verified": completed,
            "accepted_expected": len(expected), "accepted_actual": len(actual),
            "prior_accepted_preserved": len(before["processed"]),
            "human_bi_locks_preserved": len(p._load_bi_lock_snapshot(before_final))}


def run(args):
    cfg_path = Path(args.config).resolve()
    root = cfg_path.parent
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    paths = {key: (root / str(cfg["files"][key])).resolve()
             for key in ("master", "final", "cache", "classification_db", "input")}
    result = Path(args.result).resolve() if args.result else (root / cfg["files"]["output"]).resolve()
    rules = p.Rules((root / args.rules).resolve())
    for name, path in {**paths, "result": result}.items():
        if not path.is_file():
            raise RuntimeError(f"REPLAY_FILE_MISSING:{name}:{path}")

    source_rows, cohort = read_result(
        result, args.expected_rows, rules, db_path=paths["classification_db"]
    )
    before_visibility = business_visibility(source_rows, paths["final"])
    print("OFFLINE RESULT VS FINAL | " + " | ".join(
        f"{k}={v}" for k, v in before_visibility.items()
    ), flush=True)
    before_hash = {key: digest(paths[key]) for key in ("master", "final", "cache", "classification_db")}
    before_hash["result"] = digest(result)
    audit_dir = root / "auditorias"
    audit_dir.mkdir(exist_ok=True)
    staged_stats = None
    with tempfile.TemporaryDirectory(prefix="offline_replay_", dir=audit_dir) as working:
        work = Path(working)
        staged_master = work / paths["master"].name
        staged_final = work / paths["final"].name
        staged_db = work / paths["classification_db"].name
        shutil.copy2(paths["master"], staged_master)
        shutil.copy2(paths["final"], staged_final)
        sqlite_snapshot(paths["classification_db"], staged_db)
        staged_cfg = json.loads(json.dumps(cfg))
        staged_cfg["files"]["classification_db"] = str(staged_db)
        staged_cfg["files"]["baseline_backup_dir"] = str(work / "temp_backups")
        p.LOG.info("OFFLINE REPLAY STAGING | rows=%d | 0 JEV, 0 Search", len(source_rows))
        stage_stats = p.persist_baseline_excels(
            rows=source_rows, master_path=staged_master, final_path=staged_final,
            input_path=paths["input"], rules=rules,
            reprocess=False, cache_path=paths["cache"], cfg=staged_cfg)
        checks = verify(paths["master"], paths["final"],
                        staged_master, staged_final, cohort, source_rows,
                        paths["input"])
        after_visibility = business_visibility(source_rows, staged_final)
        print("STAGED RESULT VS FINAL | " + " | ".join(
            f"{k}={v}" for k, v in after_visibility.items()
        ), flush=True)
        staged_stats = {"persist": stage_stats, "checks": checks}
        # Check that staging only touched staging files.
        for key in ("master", "final", "cache", "classification_db"):
            if digest(paths[key]) != before_hash[key]:
                raise RuntimeError("OFFICIAL_CHANGED_DURING_DRY_RUN:" + key)
        if digest(result) != before_hash["result"]:
            raise RuntimeError("SOURCE_RESULT_MODIFIED_DURING_REPLAY")

        if args.commit:
            # Never overwrite open workbooks. Single-writer operation required.
            for key in ("master", "final"):
                with paths[key].open("r+b"):
                    pass
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backups = root / "Backups_Baseline" / ("offline_replay_" + stamp)
            backups.mkdir(parents=True, exist_ok=False)
            for key in ("master", "final"):
                shutil.copy2(paths[key], backups / paths[key].name)
            sqlite_snapshot(paths["classification_db"], backups / paths["classification_db"].name)
            (backups / "source-result.sha256").write_text(
                before_hash["result"], encoding="ascii")
            committed = []
            registry_refresh_started = False
            try:
                for key, staged in (("master", staged_master), ("final", staged_final)):
                    temporary = paths[key].with_name(paths[key].stem + ".replay_tmp.xlsx")
                    shutil.copy2(staged, temporary)
                    try:
                        os.replace(temporary, paths[key])
                    finally:
                        temporary.unlink(missing_ok=True)
                    committed.append(key)
                registry_refresh_started = True
                p._refresh_sqlite_sku_registry(paths["master"], paths["final"], cfg)
                if digest(paths["master"]) != digest(staged_master) or digest(paths["final"]) != digest(staged_final):
                    raise RuntimeError("POST_COMMIT_HASH_MISMATCH")
                verify(backups / paths["master"].name,
                       backups / paths["final"].name,
                       paths["master"], paths["final"], cohort, source_rows,
                       paths["input"])
            except Exception:
                for key in reversed(committed):
                    try:
                        shutil.copy2(backups / paths[key].name, paths[key])
                    except OSError:
                        p.LOG.exception("ROLLBACK FAILED for %s: restore manually from %s", key, backups)
                if registry_refresh_started:
                    try:
                        with closing(sqlite3.connect(backups / paths["classification_db"].name)) as saved:
                            with closing(sqlite3.connect(paths["classification_db"])) as destination:
                                saved.backup(destination)
                    except (OSError, sqlite3.Error):
                        p.LOG.exception(
                            "SQLITE REGISTRY ROLLBACK FAILED: recover manually from %s",
                            backups,
                        )
                raise
        output = {
            "status": "COMMITTED" if args.commit else "STAGED_PASS_NO_WRITES",
            "input_result": str(result), "expected_rows": args.expected_rows,
            "replayed_keys": len(cohort), "checks": checks,
            "visibility_before": before_visibility,
            "visibility_staged": after_visibility,
            "stage_stats": {"baseline_promoted": stage_stats.get("baseline_promoted"),
                            "review_recorded": stage_stats.get("review_recorded"),
                            "cache_reference_titles": stage_stats.get("cache_reference_titles"),
                            "final_rows": stage_stats.get("final_rows")},
            "calls_jev": 0, "calls_search": 0,
            "official_changed": bool(args.commit),
        }
        report = audit_dir / ("replay_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
        report.write_text(json.dumps(output, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(json.dumps({**output, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "pipeline_config.json"))
    parser.add_argument("--rules", default="regras_negocio.json")
    parser.add_argument("--result", default="")
    parser.add_argument("--expected-rows", type=int, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    if args.expected_rows <= 0:
        parser.error("--expected-rows must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
