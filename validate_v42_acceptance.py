#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-fast acceptance validator for V4.2 classification outputs."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import pipeline_produtos as pp


def _find_output_sheet(path: Path, requested: str = "") -> pd.DataFrame:
    if requested:
        return pd.read_excel(path, sheet_name=requested, dtype=object)
    book = pd.ExcelFile(path)
    preferred = ["Resultados", "Resultado", "Sem GLiNER", "Com GLiNER", "Classificação", "Classificacao"]
    for name in preferred:
        if name in book.sheet_names:
            return pd.read_excel(path, sheet_name=name, dtype=object)
    return pd.read_excel(path, sheet_name=book.sheet_names[0], dtype=object)


def validate(result_path: Path, gold_path: Path, sheet: str = "") -> pd.DataFrame:
    gold = pd.read_csv(gold_path, dtype=object)
    result = _find_output_sheet(result_path, sheet)

    gb = pp.find_col(gold, ["Marca", "Brand"])
    gm = pp.find_col(gold, ["Modelo", "Model"])
    gcg = pp.find_col(gold, ["Expected Category General", "Gold Category General", "Category General"])
    gf = pp.find_col(gold, ["Expected FAMILY", "Gold FAMILY", "FAMILY"])

    rb = pp.find_col(result, ["Marca", "Brand"])
    rm = pp.find_col(result, ["Modelo", "Model"])
    rcg = pp.find_col(result, ["Category General"])
    rf = pp.find_col(result, ["FAMILY", "Family"])

    if gold.empty:
        raise ValueError("GOLD_EMPTY")
    if result.empty:
        raise ValueError("RESULT_EMPTY")

    expected = {}
    for _, row in gold.iterrows():
        key = pp.business_key(pp.safe(row.get(gb)), pp.safe(row.get(gm)))
        if not pp.safe(row.get(gb)).strip() or not pp.safe(row.get(gm)).strip():
            raise ValueError("GOLD_MISSING_IDENTITY")
        if key in expected:
            raise ValueError(f"GOLD_DUPLICATE_KEY:{key}")
        if not pp.safe(row.get(gcg)).strip() or not pp.safe(row.get(gf)).strip():
            raise ValueError(f"GOLD_MISSING_TAXONOMY:{key}")
        expected[key] = {
            "Marca": pp.safe(row.get(gb)),
            "Modelo": pp.safe(row.get(gm)),
            "Expected Category General": pp.safe(row.get(gcg)).upper(),
            "Expected FAMILY": pp.safe(row.get(gf)).upper(),
        }

    actual = {}
    for _, row in result.iterrows():
        key = pp.business_key(pp.safe(row.get(rb)), pp.safe(row.get(rm)))
        if key in expected:
            if key in actual:
                raise ValueError(f"RESULT_DUPLICATE_KEY:{key}")
            actual[key] = {
                "Actual Category General": pp.safe(row.get(rcg)).upper(),
                "Actual FAMILY": pp.safe(row.get(rf)).upper(),
                "Resolution State": pp.safe(row.get("Resolution State")),
                "Decision Source": pp.safe(row.get("Decision Source")),
                "Reason Code": pp.safe(row.get("Reason Code")),
            }

    rows = []
    for key, exp in expected.items():
        act = actual.get(key, {})
        ok = (
            act.get("Actual Category General", "") == exp["Expected Category General"]
            and act.get("Actual FAMILY", "") == exp["Expected FAMILY"]
        )
        rows.append({
            **exp,
            **act,
            "PASS": "SIM" if ok else "NÃO",
            "Failure": "" if ok else (
                "MISSING_RESULT" if not act
                else f'EXPECTED={exp["Expected Category General"]}|{exp["Expected FAMILY"]}; '
                     f'ACTUAL={act.get("Actual Category General","")}|{act.get("Actual FAMILY","")}'
            ),
        })
    return pd.DataFrame(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--result", required=True)
    p.add_argument("--gold", default="acceptance/v42_gladiator_10.csv")
    p.add_argument("--sheet", default="")
    p.add_argument("--report", default="")
    args = p.parse_args()

    result_path = Path(args.result)
    gold_path = Path(args.gold)
    report = validate(result_path, gold_path, args.sheet)

    passed = int((report["PASS"] == "SIM").sum())
    total = len(report)
    failed = total - passed
    print(f"V42 ACCEPTANCE | pass={passed}/{total} | fail={failed}")
    if failed:
        print(report.loc[report["PASS"] != "SIM", [
            "Marca", "Modelo", "Expected Category General", "Expected FAMILY",
            "Actual Category General", "Actual FAMILY", "Resolution State",
            "Decision Source", "Reason Code", "Failure"
        ]].to_string(index=False))

    if args.report:
        out = Path(args.report)
        if out.suffix.lower() == ".xlsx":
            report.to_excel(out, index=False)
        else:
            report.to_csv(out, index=False, encoding="utf-8-sig")

    raise SystemExit(0 if failed == 0 else 2)


if __name__ == "__main__":
    main()
