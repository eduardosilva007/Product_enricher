#!/usr/bin/env python3
"""Offline, audited reconciliation of the 24 exported results from a committed 50-SKU campaign.

Never searches, calls an LLM, reclassifies, rewrites unrelated SKU rows, or
overrides Manual / Revisao B.I=OK. --apply creates a backup before Excel writes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from collections import defaultdict
import sys

# Direct invocation sets sys.path[0] to scripts/, not the repository root.
# Make local imports work both with python scripts/...py and python -m scripts....
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook
from sku_registry_sqlite import _key, audit_sqlite_merge

FIELDS = ("O que é (Tipo)", "Alimentação", "Category General", "FAMILY", "Título Real")
PUBLIC = (*FIELDS, "Próxima Ação", "Status", "Revisão", "Revisão B.I")


def clean(x):
    return "" if x is None else str(x).strip()


def rows(ws):
    headers = [clean(cell.value) for cell in ws[1]]
    if len([h for h in headers if h]) != len(set(h for h in headers if h)):
        raise RuntimeError("DUPLICATE_EXCEL_HEADERS")
    col = {h: i for i, h in enumerate(headers)}
    for mandatory in ("Marca", "Modelo"):
        if mandatory not in col:
            raise RuntimeError("MISSING_IDENTITY_COLUMN:" + mandatory)
    for r in ws.iter_rows(min_row=2):
        record = {name: r[i].value if i < len(r) else None for name, i in col.items()}
        brand, model = clean(record["Marca"]), clean(record["Modelo"])
        if brand and model:
            yield r[0].row, (brand, model), record


def sheet(book, preferred):
    if preferred in book.sheetnames:
        return book[preferred]
    for ws in book.worksheets:
        header = {clean(c.value) for c in ws[1]}
        if {"Marca", "Modelo"}.issubset(header):
            return ws
    raise RuntimeError("BUSINESS_SHEET_NOT_FOUND:" + preferred)


def collect_results(root, state, expected):
    campaign_root = (root / "piloto_argentina" / "controlled_night").resolve()
    if state.get("status") != "COMPLETE" or state.get("audited_products") != 50:
        raise RuntimeError("CAMPAIGN_NOT_COMPLETE_50")
    if (state.get("promotion") or {}).get("status") != "COMMITTED":
        raise RuntimeError("CAMPAIGN_NOT_COMMITTED")
    expected_results = state["promotion"].get("classification_rows")
    if expected_results != expected:
        raise RuntimeError(f"UNEXPECTED_RECORDED_RESULTS:{expected_results}; expected={expected}")

    cohort = {}
    outputs = {}
    for batch in state.get("batches", []):
        run = Path(batch["run_dir"]).resolve()
        if run.parent != campaign_root:
            raise RuntimeError("RUN_OUTSIDE_CAMPAIGN:" + str(run))
        cfg = json.loads((run / "isolated_config.json").read_text(encoding="utf-8"))
        output_path = Path(cfg["files"]["output"])
        input_path = Path(cfg["files"]["input"])
        for filename in (output_path, input_path):
            if not filename.is_file() or not filename.resolve().is_relative_to(run):
                raise RuntimeError("ISOLATED_FILE_MISSING_OR_OUTSIDE_RUN:" + str(filename))
        input_book = load_workbook(input_path, read_only=True, data_only=True)
        try:
            input_pairs = { _key(*pair): pair
                            for _, pair, _ in rows(sheet(input_book, "Base de Produtos")) }
        finally:
            input_book.close()
        batch_keys = set(batch["cohort_keys"])
        if set(input_pairs) != batch_keys or len(input_pairs) != len(batch_keys):
            raise RuntimeError("ISOLATED_INPUT_MISMATCH")
        if set(cohort).intersection(batch_keys):
            raise RuntimeError("DUPLICATE_CAMPAIGN_KEY")
        cohort.update(input_pairs)
        book = load_workbook(output_path, read_only=True, data_only=True)
        try:
            ws = sheet(book, "Resultados")
            for _, pair, record in rows(ws):
                key = _key(*pair)
                if key not in batch_keys or pair != input_pairs[key] or key in outputs:
                    raise RuntimeError("OUTPUT_IDENTITY_MISMATCH:" + key)
                outputs[key] = record
        finally:
            book.close()
    if len(cohort) != 50 or len(outputs) != expected:
        raise RuntimeError(f"CAMPAIGN_COUNT_MISMATCH:cohort={len(cohort)} outputs={len(outputs)}")
    return cohort, outputs


def complete(rec):
    t = clean(rec.get("O que é (Tipo)")).upper()
    c = clean(rec.get("Category General")).upper()
    f = clean(rec.get("FAMILY")).upper()
    return bool(t and c and f and t not in {"UNKNOWN", "REVISAR"}
                and (c, f) != ("OTHERS", "OTHERS"))


def plan(final_path, cohort, outputs):
    book = load_workbook(final_path)
    try:
        ws = sheet(book, "Base Final")
        columns = {clean(c.value): c.column for c in ws[1] if clean(c.value)}
        if not set(("Marca", "Modelo", *PUBLIC)).issubset(columns):
            raise RuntimeError("FINAL_PUBLIC_COLUMNS_MISSING")
        found = defaultdict(list)
        for row_num, pair, rec in rows(ws):
            key = _key(*pair)
            if key in cohort:
                found[key].append((row_num, pair, rec))
        if set(found) != set(cohort):
            raise RuntimeError("CAMPAIGN_KEYS_MISSING_IN_FINAL:" + str(len(set(cohort) - set(found))))
        edits = []
        statuses = {"source_records": len(outputs), "source_complete": sum(complete(x) for x in outputs.values()),
                    "protected": 0, "already_consistent": 0, "requires_update": 0,
                    "current_complete_of_50": sum(
                        any(complete(rec) for _, _, rec in matches)
                        for matches in found.values())}
        for key, source in sorted(outputs.items()):
            matches = found[key]
            exact = [(n, r) for n, pair, r in matches if pair == cohort[key]]
            if len(exact) != 1 or len(matches) != 1:
                raise RuntimeError("AMBIGUOUS_PHYSICAL_SKU:" + key)
            row_num, target = exact[0]
            if clean(target.get("Revisão B.I")).upper() == "OK" or clean(target.get("Status")).upper() == "MANUAL":
                statuses["protected"] += 1
                continue
            desired = {field: source.get(field) for field in FIELDS if clean(source.get(field))}
            # Never upgrade proposals to human approvals.
            state = clean(source.get("Resolution State")).upper()
            resolved = state in {"RESOLVED", "CLASSIFIED_CLEF"}
            desired.update({"Status": "Script", "Revisão": "PROVÁVEL" if resolved else "REVISAR",
                            "Próxima Ação": "DOUBLE_CHECK" if resolved else "REVISAR_EVIDENCIA_CACHE"})
            diffs = {field: value for field, value in desired.items()
                     if clean(target.get(field)) != clean(value)}
            if not diffs:
                statuses["already_consistent"] += 1
            else:
                statuses["requires_update"] += 1
                for field, value in diffs.items():
                    edits.append((row_num, field, value, key))
        return statuses, edits
    finally:
        book.close()


def repair(repo, campaign_id, expected, apply):
    manifest = repo / "piloto_argentina" / "controlled_night" / "campaigns" / campaign_id / "campaign.json"
    if not manifest.is_file():
        raise RuntimeError("CAMPAIGN_MANIFEST_NOT_FOUND:" + str(manifest))
    cfg = json.loads((repo / "pipeline_config.json").read_text(encoding="utf-8-sig"))
    paths = {name: (repo / cfg["files"][name]).resolve() for name in ("final", "master", "classification_db")}
    state = json.loads(manifest.read_text(encoding="utf-8"))
    cohort, output = collect_results(repo, state, expected)
    statuses, edits = plan(paths["final"], cohort, output)
    report = {"mode": "APPLY" if apply else "DRY_RUN", "campaign": campaign_id,
              "cohort": len(cohort), **statuses, "api_calls": 0}
    if not apply:
        return report
    if statuses["protected"]:
        report["protected_not_overwritten"] = statuses["protected"]
    if edits:
        backup = manifest.parent / "final_before_24_result_reconciliation.xlsx"
        if backup.exists():
            raise RuntimeError("EXISTING_REPAIR_BACKUP: manual inspection required")
        shutil.copy2(paths["final"], backup)
        fd, name = tempfile.mkstemp(prefix="final_repair_", suffix=".xlsx", dir=paths["final"].parent)
        os.close(fd)
        temp = Path(name)
        try:
            shutil.copy2(paths["final"], temp)
            book = load_workbook(temp)
            try:
                ws = sheet(book, "Base Final")
                columns = {clean(c.value): c.column for c in ws[1] if clean(c.value)}
                for row_no, field, value, _ in edits:
                    ws.cell(row_no, columns[field]).value = value
                book.save(temp)
            finally:
                book.close()
            checked, remaining = plan(temp, cohort, output)
            if checked["requires_update"] or checked["protected"] != statuses["protected"]:
                raise RuntimeError("POST_STAGE_RESULT_MISMATCH")
            os.replace(temp, paths["final"])
        finally:
            if temp.exists():
                temp.unlink()
        report["backup"] = str(backup)
    else:
        report["backup"] = None
    # Canonical SQLite lookup is a projection of the official Excel, not its writer.
    sqlite_result = audit_sqlite_merge(paths["final"], paths["master"],
                                       paths["classification_db"], set(cohort), commit=True)
    if sqlite_result["status"] != "SQLITE_MERGED":
        raise RuntimeError("SQLITE_PROJECTION_SYNC_FAILED:" + sqlite_result["status"])
    after, remaining = plan(paths["final"], cohort, output)
    if after["requires_update"] or after["protected"] != statuses["protected"]:
        raise RuntimeError("POST_COMMIT_VALIDATION_FAILED")
    report.update({"status": "PASS", "now_consistent": after["already_consistent"],
                   "final_complete_of_50": after["current_complete_of_50"],
                   "sqlite_status": sqlite_result["status"]})
    return report


def main():
    p = argparse.ArgumentParser(description="Offline repair/verification of 24 results -> official Base Final")
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--campaign", default="20261113T124106Z_2d6e7cfe")
    p.add_argument("--expected", type=int, default=24)
    p.add_argument("--apply", action="store_true", help="Back up official Excel and apply only missing/different source fields")
    a = p.parse_args()
    try:
        result = repair(a.repo.resolve(), a.campaign, a.expected, a.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "api_calls": 0}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
