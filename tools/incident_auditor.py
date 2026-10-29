#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_produtos as pp


def output_sheet(path: Path):
    book = pd.ExcelFile(path)
    for name in ["Resultados", "Resultado", "Sem GLiNER", "Com GLiNER"]:
        if name in book.sheet_names:
            return pd.read_excel(path, sheet_name=name, dtype=object)
    return pd.read_excel(path, sheet_name=book.sheet_names[0], dtype=object)


def incident(row) -> str:
    state = pp.safe(row.get("Resolution State")).upper()
    identity = pp.safe(row.get("Identity")).upper()
    role = pp.safe(row.get("Product Role")).upper()
    source = pp.safe(row.get("Decision Source")).upper()
    reason = pp.safe(row.get("Reason Code")).upper()

    if state == "OPERATIONAL_ERROR" or "ERROR" in reason:
        return "OPERATIONAL_ERROR"
    if identity and identity != "TARGET_CONFIRMED":
        return "IDENTITY_ERROR"
    if state == "NEEDS_EVIDENCE":
        return "EVIDENCE_ERROR"
    if role == "UNKNOWN":
        return "ROLE_ERROR"
    if "GLINER" in source:
        return "ENTITY_EXTRACTION_ERROR"
    if "BGE" in source or "SEMANTIC" in reason:
        return "SEMANTIC_RETRIEVAL_ERROR"
    if state == "CLASSIFIED_LLM":
        return "DECISION_PROVIDER_ERROR"
    if pp.safe(row.get("Category General")) in {"", "OTHERS"} and pp.safe(row.get("FAMILY")) in {"", "OTHERS"}:
        return "TAXONOMY_MAPPING_ERROR"
    return "RULE_ERROR"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--result", required=True)
    p.add_argument("--output", default="")
    args = p.parse_args()

    path = Path(args.result)
    if not path.is_absolute():
        path = ROOT / path
    df = output_sheet(path).copy()
    df["Incident Class"] = df.apply(incident, axis=1)

    cols = [c for c in [
        "Marca", "Modelo", "Resolution State", "Identity", "Product Role",
        "Decision Source", "Reason Code", "Incident Class"
    ] if c in df.columns]
    print(df[cols].to_string(index=False))

    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = ROOT / out
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print("REPORT:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
