#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final independent mass-run gate. Requires real 50-SKU technical PASS and 10 manually OK reviews."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from openpyxl import load_workbook


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check(report, source):
    report = Path(report).resolve()
    source = Path(source).resolve()
    result = json.loads(report.read_text(encoding="utf-8"))
    if result.get("status") != "TECHNICAL_PASS_AWAITING_QUALITY_REVIEW":
        raise RuntimeError("PILOT_50_TECHNICAL_STATUS_NOT_APPROVED")
    metrics = result.get("audited") or {}
    if int(metrics.get("complete_classifications", 0)) < 35:
        raise RuntimeError("PILOT_50_CLASSIFICATION_YIELD_TOO_LOW")
    if int(metrics.get("search_titles_missing_in_final", -1)) != 0:
        raise RuntimeError("PILOT_50_TITLE_COVERAGE_BLOCKED")
    if int(metrics.get("bi_locks_preserved", -1)) < 0:
        raise RuntimeError("PILOT_50_HUMAN_LOCK_AUDIT_MISSING")
    if int(metrics.get("prior_accepted_preserved", -1)) < 0:
        raise RuntimeError("PILOT_50_PRIOR_ACCEPTED_AUDIT_MISSING")

    pilot_dir = report.parent
    manifest = json.loads((pilot_dir / "manifest.json").read_text(encoding="utf-8"))
    if Path(manifest["source"]).resolve() != source:
        raise RuntimeError("MASS_SOURCE_DOES_NOT_MATCH_PILOT_SOURCE")
    if digest(source) != manifest["source_sha256"]:
        raise RuntimeError("MASS_SOURCE_CHANGED_AFTER_PILOT_50")
    if manifest["state"] != "TECHNICAL_PASS_AWAITING_QUALITY_REVIEW":
        raise RuntimeError("PILOT_MANIFEST_NOT_TECHNICALLY_PASSED")
    quality_path = Path(metrics["quality_review_file"]).resolve()
    if quality_path.parent != pilot_dir:
        raise RuntimeError("PILOT_QA_REPORT_OUTSIDE_PILOT_DIRECTORY")
    if not quality_path.is_file():
        raise RuntimeError("PILOT_QA_REPORT_NOT_FOUND")
    book = load_workbook(quality_path, read_only=True, data_only=True)
    try:
        if "Amostra_10_Validar" not in book.sheetnames or "Todos_50" not in book.sheetnames:
            raise RuntimeError("PILOT_QA_REQUIRED_SHEETS_MISSING")
        sheet = book["Amostra_10_Validar"]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, ())
        column = {str(x).strip():i for i,x in enumerate(header) if x is not None}
        required = ("Marca","Modelo","Auditoria Manual (OK/ERRO)",
                    "Título Real na Base Final","O que é (Tipo)","Category General","FAMILY")
        if not set(required).issubset(column):
            raise RuntimeError("PILOT_QA_REQUIRED_FIELDS_MISSING")
        data = [row for row in rows if any(x is not None for x in row)]
        if len(data) != 10:
            raise RuntimeError(f"PILOT_QA_SAMPLE_COUNT:{len(data)}!=10")
        seen = set()
        for row in data:
            pair = (str(row[column["Marca"]]).strip(), str(row[column["Modelo"]]).strip())
            if pair in seen:
                raise RuntimeError("PILOT_QA_DUPLICATE_SKU:" + repr(pair))
            seen.add(pair)
            review = str(row[column["Auditoria Manual (OK/ERRO)"]] or "").strip().upper()
            if review != "OK":
                raise RuntimeError("PILOT_QA_NOT_MANUALLY_APPROVED:" + repr(pair))
    finally:
        book.close()
    return {"status":"APPROVED_FOR_BOUNDED_MASS_RUN",
            "source":str(source),"pilot_report":str(report),
            "pilot_complete":int(metrics["complete_classifications"]),
            "human_reviews_approved":10,"new_search_calls":0,"jev_calls":0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",required=True)
    parser.add_argument("--source",required=True)
    args=parser.parse_args(argv)
    try:
        data=check(args.report,args.source)
    except Exception as exc:
        print(f"PILOT_50_RELEASE_GATE: BLOCKED | {type(exc).__name__}: {exc}")
        return 2
    print(json.dumps(data,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
