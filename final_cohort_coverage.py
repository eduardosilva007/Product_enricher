#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify physical exact Marca+Modelo rows in a committed Final XLSX.

Never confuse 'accepted classifications' or normalized business keys with
actual row coverage. A missing or unreadable workbook fails closed.
"""
from collections import Counter
from pathlib import Path


def _present_rows(path):
    from openpyxl import load_workbook
    if not Path(path).is_file():
        raise RuntimeError("FULL_FINAL_WORKBOOK_MISSING")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            head = next(rows, ())
            keys = {str(x).strip().casefold(): i for i, x in enumerate(head) if x is not None}
            if "marca" not in keys or "modelo" not in keys:
                continue
            b, m = keys["marca"], keys["modelo"]
            found = Counter()
            for row in rows:
                brand = str(row[b]).strip() if len(row) > b and row[b] is not None else ""
                model = str(row[m]).strip() if len(row) > m and row[m] is not None else ""
                if brand and model:
                    found[(brand, model)] += 1
            return found
        raise RuntimeError("FULL_FINAL_BUSINESS_SHEET_NOT_FOUND")
    finally:
        wb.close()


def audit_final_cohort(path, cohort):
    required = Counter()
    for row in cohort:
        if isinstance(row, dict):
            brand, model = row.get("Marca", ""), row.get("Modelo", "")
        else:
            brand, model = row[0], row[1]
        brand, model = str(brand).strip(), str(model).strip()
        if not brand or not model:
            raise RuntimeError("FULL_FINAL_COHORT_INVALID_IDENTITY")
        required[(brand, model)] = 1
    present = _present_rows(path)
    missing = [(brand, model) for (brand, model), count in required.items()
               if present[(brand, model)] < count]
    report = {
        "attempted": len(cohort),
        "unique_exact": len(required),
        "present": len(required)-len(missing),
        "missing": len(missing),
        "status": "PASS" if not missing else "FAIL",
    }
    if missing:
        raise RuntimeError(
            "FULL_FINAL_ROW_COVERAGE_FAILED: "
            f"{report['present']}/{len(required)} exact SKU rows found; "
            f"missing={missing[:8]}"
        )
    return report
