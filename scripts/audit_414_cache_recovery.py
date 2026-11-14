#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Triage every REVISAR_EVIDENCIA_CACHE in the CURRENT INPUT using RAW SQLite.

Zero Search/LLM, no mutation to official files. Emits per-SKU CSV and JSON
for offline salvage. Does NOT promote or approve any classification.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p

FIELDS = (
    "Marca", "Modelo", "Route", "Action", "HARD_LOCK", "Revisão B.I",
    "Tipo Final", "Category Final", "Family Final", "Final Título",
    "Master Resolution", "Master Stage", "Master Reason Code",
    "RAW State", "Collection State", "RAW Results", "Curated Sources",
    "Safe Auxiliary", "Cache Title", "Cache Title Example",
)


def route_one(final, raw, master=None):
    master = master or {}
    payload = raw.payload or {}
    collection = payload.get("_collection")
    collection = collection if isinstance(collection, dict) else {}
    raw_items = [x for x in (payload.get("results") or []) if isinstance(x, dict)]
    curated = [x for x in (collection.get("curated_results") or []) if isinstance(x, dict)]
    ev = [x for x in (collection.get("evidence_cache") or []) if isinstance(x, dict)]
    safe_aux = [
        x for x in ev if p.safe(x.get("tier")).upper() == "AUXILIARY_REVIEW_ONLY"
        and p.safe(x.get("identity")).upper() in {"STRONG", "PROBABLE"}
        and x.get("eligible_for_llm_rescue", True) is not False
        and p.safe(x.get("decision")).upper() not in
        {"REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"}
    ]
    title = next((
        p.safe(x.get("title")) for x in curated + raw_items
        if p.safe(x.get("title"))
    ), "")
    locked = p.safe(final.get("Revisão B.I")).upper() == "OK" or p.safe(final.get("Status")).upper() == "MANUAL"
    accepted_master = (
        p.safe(master.get("Resolution State")).upper() in {"RESOLVED", "CLASSIFIED_CLEF"}
        and all(p.safe(master.get(x)) for x in ("O que é (Tipo)", "Category General", "FAMILY"))
        and p.safe(master.get("O que é (Tipo)")).upper() not in {"REVISAR", "UNKNOWN"}
    )
    if locked:
        label = "01_HUMAN_LOCK"
        action = "PRESERVAR; revisão humana específica, nenhuma mudança automática"
    elif accepted_master:
        label = "02_MASTER_ALREADY_CLASSIFIED"
        action = "Conferir diferença MASTER vs Base Final antes de qualquer LLM"
    elif raw.state in {"RAW_OK", "RAW_OK_MERGED"} and (
            p.safe(collection.get("state")) in {"READY_FOR_CLASSIFICATION", "PARTIAL_TARGET_EVIDENCE"}
            or curated or safe_aux):
        label = "03_CLASSIFIABLE_NO_SEARCH"
        action = "Elegível a classificação usando cache, sem nova Search"
    elif raw.state in {"RAW_OK", "RAW_OK_MERGED"} and raw_items:
        label = "04_RECURATE_OFFLINE"
        action = "Revisar resultados RAW e identidade; não chamar JEV sem evidence gate"
    elif raw.state in {"RAW_OK", "RAW_OK_MERGED"} and ev:
        label = "05_CACHE_SOURCES_REJECTED"
        action = "Revisar decisões das fontes; não aprovar fonte rejeitada"
    elif raw.state in {"RAW_OK", "RAW_OK_MERGED"}:
        label = "06_RAW_OK_NO_USABLE_RESULTS"
        action = "Investigar cache/resultado antes de nova busca"
    elif raw.state == "RAW_EMPTY":
        label = "07_RAW_EMPTY_ATTEMPTED"
        action = "Não recoletar automaticamente; verificar ledger e autorização"
    elif raw.state == "RAW_MISSING":
        label = "08_RAW_MISSING"
        action = "Elegível a primeira Search se não constar no ledger"
    else:
        label = "09_CACHE_ERROR"
        action = "Auditar corrupção/colisão antes de qualquer chamada"
    return {
        "Route": label, "Action": action, "HARD_LOCK": bool(locked),
        "Master Resolution": p.safe(master.get("Resolution State")),
        "Master Stage": p.safe(master.get("Stage")),
        "Master Reason Code": p.safe(master.get("Reason Code")),
        "RAW State": raw.state,
        "Collection State": p.safe(collection.get("state")),
        "RAW Results": len(raw_items), "Curated Sources": len(curated),
        "Safe Auxiliary": len(safe_aux),
        "Cache Title": bool(title), "Cache Title Example": title[:400],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "pipeline_config.json"))
    parser.add_argument("--input", default="", help="Original 1030-SKU input; does not write it")
    parser.add_argument("--output-dir", default=str(ROOT / "auditorias"))
    args = parser.parse_args(argv)
    import pandas as pd
    config = Path(args.config).resolve()
    root = config.parent
    files = json.loads(config.read_text(encoding="utf-8-sig"))["files"]
    paths = {k: (root / files[k]).resolve() for k in ("final", "master", "cache", "input")}
    if args.input:
        paths["input"] = Path(args.input).resolve()
    for key, path in paths.items():
        if not path.is_file():
            raise RuntimeError(f"INPUT_FILE_NOT_FOUND:{key}:{path}")
    wanted = {(p.safe(r.brand_original), p.safe(r.model_original))
              for r in p.read_input_rows(paths["input"]) if r.valid}
    final, _ = p._read_business_sheet(paths["final"])
    master, _ = p._read_business_sheet(paths["master"])
    masters = {(p.safe(x.get("Marca")), p.safe(x.get("Modelo"))): x
               for _, x in master.iterrows()}
    subset = []
    for _, x in final.iterrows():
        key = (p.safe(x.get("Marca")), p.safe(x.get("Modelo")))
        if key in wanted and p.safe(x.get("Próxima Ação")).upper() == "REVISAR_EVIDENCIA_CACHE":
            subset.append((key, x))
    unique = {x[0] for x in subset}
    if len(unique) != len(subset):
        raise RuntimeError(f"DUPLICATE_PHYSICAL_FINAL_PAIRS:{len(subset)}_vs_{len(unique)}")
    rows = []
    with closing(p.TavilyReadOnlyStore(paths["cache"])) as cache:
        for (brand, model), final_row in subset:
            item = route_one(final_row, cache.get(brand, model),
                             masters.get((brand, model)))
            row = {
                "Marca": brand, "Modelo": model,
                "Revisão B.I": p.safe(final_row.get("Revisão B.I")),
                "Tipo Final": p.safe(final_row.get("O que é (Tipo)")),
                "Category Final": p.safe(final_row.get("Category General")),
                "Family Final": p.safe(final_row.get("FAMILY")),
                "Final Título": p.safe(final_row.get("Título Real")),
                **item,
            }
            rows.append(row)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"triagem_414_{stamp}.csv"
    json_path = out / f"triagem_414_{stamp}.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter=";", fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "status": "READ_ONLY", "input_unique_pairs": len(wanted),
        "revisar_evidencia_cache_count": len(rows),
        "routes": dict(sorted(Counter(x["Route"] for x in rows).items())),
        "raw_states": dict(sorted(Counter(x["RAW State"] for x in rows).items())),
        "raw_with_title": sum(bool(x["Cache Title"]) for x in rows),
        "final_with_title": sum(bool(x["Final Título"]) for x in rows),
        "calls_search": 0, "calls_jev": 0, "canonical_files_changed": False,
        "csv": str(csv_path), "json": str(json_path),
    }
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
