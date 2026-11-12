#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline cache triage: read-only cache/master/final; writes only a diagnostic CSV/JSON.

Usage:
 python scripts/audit_cache_reuse.py
 python scripts/audit_cache_reuse.py --all-pending
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p

# The 24 audit examples are a tracing cohort, NOT a request to overwrite gold.
AUDITED_24 = (
    ("CNH", "81868398"), ("CNH", "944005237"), ("CNH", "47909150"),
    ("CUMMINS", "GTA38GC"), ("CUMMINS", "0130 4748"), ("COOFIX", "CF EG006 ES"),
    ("CNH", "47951815"), ("CNH", "92680566"),
    ("DCA", "ADJZ1201E 10.8V 2.0AH*2"),
    ("DARTAY", "HAND SPRAYER 1.5 L"), ("DARTAY", "HAND SPRAYER 2L"),
    ("COMET", "K250TSR13 190T CLASS"), ("COMET", "KS25010 150 CLASSIC"),
    ("COOFIX", "CF GWP002"),
    ("CNH", "51640073"), ("CNH", "92655456"), ("CUMMINS", "A075T322"),
    ("COOFIX", "CF EG001C"), ("CNH", "48137950"), ("CNH", "84420264"),
    ("CNH", "47945518"), ("CNH", "87014923"), ("CNH", "91715868"),
    ("CNH", "47507106"),
)
FIELDS = [
    "Marca", "Modelo", "Próxima Ação", "Revisão", "Revisão B.I",
    "Tipo Atual", "Category General Atual", "FAMILY Atual", "Título Atual",
    "RAW State", "Collection State", "Previous Collection State",
    "Provider", "Previous Error", "RAW Results", "Curated Results",
    "Evidence Cache", "Safe Auxiliary Evidence", "Identity Strong Sources",
    "Identity Probable Sources", "Rejected Sources", "Reference Title",
    "Reference Identity", "Historical Reservation",
    "Triagem", "Justificativa",
]


def cache_triage(raw, has_title, has_labels, curated, aux_safe, strong, probable):
    if raw.state == "RAW_EMPTY":
        return ("RAW_EMPTY_WITH_LEGACY_CLASSIFICATION" if has_labels or has_title else "RAW_EMPTY",
                "RAW indisponível; título/classificação anterior não comprova RAW recuperável")
    if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
        return "CACHE_NOT_USABLE", "Cache sem evidência RAW utilizável"
    if curated and (strong or probable):
        return "READY_CURATED_RECLASSIFICATION", "Fonte curada + identidade potencial; rodar gates do classificador"
    if aux_safe:
        return "SAFE_TITLE_RESCUE_CANDIDATE", "Evidência auxiliar admitida para revisão; não autoriza aprovação automática"
    if curated:
        return "CURATED_IDENTITY_REVIEW", "Há fontes curadas, mas a identidade requer revisão"
    return "CACHE_EVIDENCE_REVIEW", "RAW existe, porém não há fonte elegível à classificação"


def collect_reservations(db: Path):
    if not db.is_file():
        return set(), "LEDGER_NOT_FOUND"
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='collection_attempt_reservations'"
        ).fetchone()
        if not check:
            return set(), "LEDGER_TABLE_NOT_FOUND"
        return {str(r[0]) for r in conn.execute("SELECT business_key FROM collection_attempt_reservations")}, "OK"


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--all-pending", action="store_true",
                      help="Inspect all pending cache actions in the Consolidado, not only the audited 24")
    args.add_argument("--output-dir", default="piloto_argentina/cache_reuse_audit")
    cli = args.parse_args()
    cfg = json.loads((ROOT / "pipeline_config.json").read_text(encoding="utf-8-sig"))
    paths = p.source_paths(cfg)
    source = paths["master"] if paths["master"].is_file() else paths["final"]
    if not source.is_file():
        raise FileNotFoundError(f"Consolidado/Base Final ausente: {source}")
    df, _ = p._read_business_sheet(source)
    brand_col = p.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    model_col = p.find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    by_key = {}
    for _, r in df.iterrows():
        b, m = p.safe(r.get(brand_col)), p.safe(r.get(model_col))
        if b and m:
            by_key.setdefault(p.business_key(b, m), r)
    selected = {}
    if cli.all_pending:
        selected = {key: (p.safe(row.get(brand_col)), p.safe(row.get(model_col)))
                    for key, row in by_key.items()
                    if p.safe(row.get("Próxima Ação")).upper() in
                    {"REVISAR_EVIDENCIA_CACHE", "CLASSIFICAR_CACHE"}}
    else:
        selected = {p.business_key(b, m): (b, m) for b, m in AUDITED_24}
    reservations, ledger_status = collect_reservations(paths["classification_db"])
    output = []
    with p.TavilyReadOnlyStore(paths["cache"]) as store:
        for key, (brand, model) in selected.items():
            source_row = by_key.get(key)
            row = source_row if source_row is not None else {}
            raw = store.get(brand, model)
            payload = raw.payload or {}
            coll = payload.get("_collection") if isinstance(payload.get("_collection"), dict) else {}
            results = [x for x in payload.get("results", []) or [] if isinstance(x, dict)]
            curated = [x for x in coll.get("curated_results", []) or [] if isinstance(x, dict)]
            evidence = [x for x in coll.get("evidence_cache", []) or [] if isinstance(x, dict)]
            audits = [x for x in coll.get("source_audit", []) or [] if isinstance(x, dict)]
            aux_safe = sum(
                p.safe(x.get("tier")).upper() == "AUXILIARY_REVIEW_ONLY"
                and p.safe(x.get("identity")).upper() in {"STRONG", "PROBABLE"}
                and x.get("eligible_for_llm_rescue", True) is not False
                and p.safe(x.get("decision")).upper() not in
                {"REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"}
                for x in evidence
            )
            strong = sum(p.safe(x.get("identity")).upper() == "STRONG" for x in evidence)
            probable = sum(p.safe(x.get("identity")).upper() == "PROBABLE" for x in evidence)
            rejected = sum(p.safe(x.get("decision")).upper().startswith("REJECT") for x in audits)
            title = p.safe(row.get("Título Real")) or p.safe(row.get("Título Referência"))
            typ = p.safe(row.get("O que é (Tipo)"))
            cg, fam = p.safe(row.get("Category General")), p.safe(row.get("FAMILY"))
            has_labels = bool(typ not in {"", "REVISAR", "UNKNOWN"} and cg and fam)
            triage, why = cache_triage(raw, bool(title), has_labels,
                                       len(curated), aux_safe, strong, probable)
            meta = payload.get("_search_meta") if isinstance(payload.get("_search_meta"), dict) else {}
            ref = p._manual_review_bundle(raw) if raw.state in {"RAW_OK", "RAW_OK_MERGED"} else {}
            output.append(dict(zip(FIELDS, [
                brand, model, p.safe(row.get("Próxima Ação")), p.safe(row.get("Revisão")),
                p.safe(row.get("Revisão B.I")), typ, cg, fam, title, raw.state,
                p.safe(coll.get("state")), p.safe(coll.get("prior_state")),
                p.safe(coll.get("provider") or meta.get("provider")),
                p.safe(coll.get("error") or meta.get("error"))[:300],
                len(results), len(curated), len(evidence), aux_safe, strong,
                probable, rejected, p.safe(ref.get("Título Referência")),
                p.safe(ref.get("Título Referência Identity")),
                "SIM" if key in reservations else "NÃO", triage, why
            ])))
    target = (ROOT / cli.output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    report_csv = target / "triagem_cache.csv"
    report_json = target / "triagem_cache_resumo.json"
    with report_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(output)
    summary = {
        "scope": "all_pending" if cli.all_pending else "audited_24",
        "audited": len(output), "ledger_status": ledger_status,
        "triage": dict(sorted(Counter(x["Triagem"] for x in output).items())),
        "raw": dict(sorted(Counter(x["RAW State"] for x in output).items())),
        "reported_actions": dict(sorted(Counter(x["Próxima Ação"] for x in output).items())),
        "search_calls": 0, "jev_calls": 0, "official_workbooks_modified": False,
        "report": str(report_csv),
    }
    report_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
