#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only reconciliation: collected SKUs -> evidence -> classifier -> canonical Final.

No Search or LLM API calls, no baseline writes. Never upgrades source-only
classifications to approved status. Run AFTER the current pipeline process exits.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline_produtos as p

CORE = ("O que é (Tipo)", "Category General", "FAMILY")
FIELDS = (
    "Marca", "Modelo", "Business Key", "Aparece na entrada",
    "Requests no relatório", "RAW Results no relatório", "Curated no relatório",
    "Collection State relatório", "RAW State atual", "Collection State atual",
    "Curated atual", "Auxiliary safe atual", "Pode entrar na fila",
    "Reserva Search anterior", "HARD LOCK", "Fonte resultado",
    "Resolution State fonte", "Resultado completo fonte", "Resultado no arquivo Excel",
    "Resultado no SQLite", "Par exato na Base Final", "Linhas físicas Base Final",
    "Classificação completa na Base Final", "Aceito automaticamente na Base Final",
    "Comparação campos com fonte", "Estado reconciliação", "O que é (Tipo) Final",
    "Category General Final", "FAMILY Final", "Título Real Final",
    "Revisão Final", "Revisão B.I Final",
)

def valid_classification(rec):
    return bool(rec and all(p.safe(rec.get(c)) for c in CORE) and
                p.safe(rec.get("O que é (Tipo)")).upper() not in
                {"REVISAR", "UNKNOWN", "N/A"} and
                p.safe(rec.get("Resolution State")).upper() in
                {"RESOLVED", "CLASSIFIED_CLEF"})

def has_final_labels(rec):
    return bool(rec and all(p.safe(rec.get(c)) for c in CORE) and
                p.safe(rec.get("O que é (Tipo)")).upper() not in
                {"REVISAR", "UNKNOWN", "N/A"})

def evidence_ready(raw):
    if raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
        return False, 0, 0, ""
    coll = (raw.payload or {}).get("_collection")
    coll = coll if isinstance(coll, dict) else {}
    curated = [x for x in (coll.get("curated_results") or []) if isinstance(x, dict)]
    auxiliary = [
        x for x in (coll.get("evidence_cache") or [])
        if isinstance(x, dict) and p.safe(x.get("tier")) == "AUXILIARY_REVIEW_ONLY"
        and p.safe(x.get("identity")) in {"STRONG", "PROBABLE"}
        and bool(x.get("eligible_for_llm_rescue", True))
        and p.safe(x.get("decision")) not in {
            "REJECT_WRONG_MODEL", "REJECT_COMPATIBILITY_ONLY", "REJECT_RELATED_ONLY"
        }
    ]
    state = p.safe(coll.get("state"))
    allowed = bool(state in {"READY_FOR_CLASSIFICATION", "PARTIAL_TARGET_EVIDENCE"}
                   or curated or auxiliary)
    return allowed, len(curated), len(auxiliary), state

def rows_by_pair(path):
    df, sheet = p._read_business_sheet(path)
    b = p.find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"])
    m = p.find_col(df, ["Modelo", "Model", "SKU", "Part Number"])
    pairs = defaultdict(list)
    for idx, row in df.iterrows():
        brand, model = p.safe(row.get(b)), p.safe(row.get(m))
        if brand and model:
            pairs[(brand, model)].append((int(idx) + 2, row.to_dict(),
                                          bool(p._baseline_row_accepted(row, df))))
    return pairs, sheet

def load_results(path):
    if not path.is_file():
        return {}, 0, "AUSENTE"
    import pandas as pd
    frame = pd.read_excel(path, sheet_name="Resultados",
                          dtype=object, keep_default_na=False)
    rows = defaultdict(list)
    for _, x in frame.iterrows():
        rec = x.to_dict()
        pair = (p.safe(rec.get("Marca")), p.safe(rec.get("Modelo")))
        if pair[0] and pair[1]:
            rows[pair].append(rec)
    return rows, len(frame), "OK"

def sqlite_history(path, keys):
    newest = {}
    reservations = set()
    runs = []
    if not path.is_file():
        return newest, reservations, runs, "DATABASE_MISSING"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("CLASSIFICATION_SQLITE_QUICK_CHECK_FAILED")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "collection_attempt_reservations" in tables:
            reservations = {x[0] for x in db.execute(
                "SELECT business_key FROM collection_attempt_reservations") if x[0] in keys}
        if "runs" in tables:
            for row in db.execute(
                    "SELECT run_id,mode,status,started_at,finished_at "
                    "FROM runs ORDER BY started_at DESC LIMIT 6"):
                runs.append(dict(row))
        if "classification_results" in tables:
            for row in db.execute(
                "SELECT brand,model,business_key,result_json,updated_at,rules_hash "
                "FROM classification_results ORDER BY updated_at"):
                key = p.safe(row["business_key"])
                if key not in keys:
                    continue
                try:
                    rec = json.loads(row["result_json"])
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                pair = (p.safe(row["brand"]), p.safe(row["model"]))
                # Keep a valid completed checkpoint over later incomplete snapshots.
                old = newest.get(pair)
                if (old is None or (valid_classification(rec), p.safe(row["updated_at"]))
                        >= (valid_classification(old["result"]), old["updated_at"])):
                    newest[pair] = {
                        "result": rec, "updated_at": p.safe(row["updated_at"]),
                        "rules_hash": p.safe(row["rules_hash"]),
                    }
    return newest, reservations, runs, "OK"

def classify_reconciliation(*, report, input_pairs, final_pairs, output_rows,
                            checkpoint_rows, reservations, cache_reader):
    results = []
    for item in report:
        pair = (item["Marca"], item["Modelo"])
        key = p.business_key(*pair)
        raw = cache_reader.get(*pair)
        ready, curated, aux, state = evidence_ready(raw)
        final_matches = final_pairs.get(pair, [])
        output = output_rows.get(pair, [])
        checkpoint = checkpoint_rows.get(pair)
        # Exact pairs only. A normalized key may represent several physical SKUs.
        source = output[-1] if output else (checkpoint or {}).get("result")
        source_complete = valid_classification(source)
        final_complete = [x for x in final_matches if has_final_labels(x[1])]
        final_locked = any(
            p.safe(x[1].get("Revisão B.I")).upper() == "OK" or
            p.safe(x[1].get("Status")).upper() == "MANUAL"
            for x in final_matches
        )
        final_accepted = any(x[2] for x in final_matches)
        matched = any(
            all(p.safe(x[1].get(c)) == p.safe(source.get(c)) for c in CORE)
            for x in final_matches
        ) if source_complete else False
        conflict = bool(source_complete and final_matches and any(
            p.safe(x[1].get(c)) and p.safe(source.get(c)) and
            p.safe(x[1].get(c)) != p.safe(source.get(c))
            for x in final_matches for c in CORE))
        if conflict:
            status = "CONFLITO_FONTE_FINAL"
        elif source_complete and matched:
            status = "CLASSIFICADO_E_GRAVADO_CONFERIDO"
        elif source_complete:
            status = "CLASSIFICADO_SEM_GRAVACAO_CONFIRMADA"
        elif final_locked:
            status = "BLOQUEADO_POR_REVISAO_HUMANA"
        elif final_complete:
            status = "FINAL_COMPLETA_SEM_FONTE_ATUAL_CONFERIDA"
        elif source:
            status = "RESULTADO_PARCIAL_OU_REVIEW"
        elif raw.state not in {"RAW_OK", "RAW_OK_MERGED"}:
            status = "RAW_INDISPONIVEL"
        elif not ready:
            status = "SEM_EVIDENCIA_CLASSIFICAVEL"
        elif pair not in input_pairs:
            status = "FORA_DA_ENTRADA_ATUAL"
        else:
            status = "ELEGIVEL_SEM_RESULTADO_GRAVADO"
        representative = final_matches[-1][1] if final_matches else {}
        results.append({
            "Marca": pair[0], "Modelo": pair[1], "Business Key": key,
            "Aparece na entrada": pair in input_pairs,
            "Requests no relatório": item["Requests"],
            "RAW Results no relatório": item["RAW Results"],
            "Curated no relatório": item["Curated Sources"],
            "Collection State relatório": item["Collection State"],
            "RAW State atual": raw.state, "Collection State atual": state,
            "Curated atual": curated, "Auxiliary safe atual": aux,
            "Pode entrar na fila": bool(ready and not final_locked),
            "Reserva Search anterior": key in reservations,
            "HARD LOCK": final_locked,
            "Fonte resultado": "EXCEL_ATUAL" if output else "SQLITE_HISTORICO" if checkpoint else "",
            "Resolution State fonte": p.safe((source or {}).get("Resolution State")),
            "Resultado completo fonte": source_complete,
            "Resultado no arquivo Excel": bool(output),
            "Resultado no SQLite": bool(checkpoint),
            "Par exato na Base Final": bool(final_matches),
            "Linhas físicas Base Final": len(final_matches),
            "Classificação completa na Base Final": bool(final_complete),
            "Aceito automaticamente na Base Final": final_accepted,
            "Comparação campos com fonte": "CONFLITO" if conflict else "IGUAL" if matched else "NAO_CONFIRMADO",
            "Estado reconciliação": status,
            "O que é (Tipo) Final": p.safe(representative.get("O que é (Tipo)")),
            "Category General Final": p.safe(representative.get("Category General")),
            "FAMILY Final": p.safe(representative.get("FAMILY")),
            "Título Real Final": p.safe(representative.get("Título Real")),
            "Revisão Final": p.safe(representative.get("Revisão")),
            "Revisão B.I Final": p.safe(representative.get("Revisão B.I")),
        })
    return results

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "pipeline_config.json"))
    parser.add_argument("--report", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "auditorias"))
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    files = cfg["files"]
    base = Path(args.config).resolve().parent
    path = lambda value: (base / str(value)).resolve()
    report_path = path(args.report or files["collection_report"])
    input_path = path(files["input"])
    final_path = path(files["final"])
    cache_path = path(files["cache"])
    sqlite_path = path(files["classification_db"])
    output_path = path(args.result or files["output"])
    for label, candidate in (("REPORT", report_path), ("INPUT", input_path),
                             ("FINAL", final_path), ("CACHE", cache_path),
                             ("SQLITE", sqlite_path)):
        if not candidate.is_file():
            raise RuntimeError(f"AUDIT_SOURCE_MISSING:{label}:{candidate}")
    import pandas as pd
    collection = pd.read_excel(report_path, sheet_name="Produtos",
                               dtype=object, keep_default_na=False)
    needed = {"Marca", "Modelo", "Provider Requests", "RAW Results",
              "Curated Sources", "Collection State"}
    if not needed.issubset(collection.columns):
        raise RuntimeError(f"COLLECTION_REPORT_COLUMNS_MISSING:{sorted(needed-set(collection.columns))}")
    report_rows, seen = [], set()
    for _, row in collection.iterrows():
        b, m = p.safe(row["Marca"]), p.safe(row["Modelo"])
        if not b or not m:
            continue
        pair = (b, m)
        if pair in seen:
            raise RuntimeError(f"COLLECTION_REPORT_DUPLICATE_PAIR:{pair}")
        seen.add(pair)
        report_rows.append({"Marca": b, "Modelo": m,
                            "Requests": row["Provider Requests"],
                            "RAW Results": row["RAW Results"],
                            "Curated Sources": row["Curated Sources"],
                            "Collection State": p.safe(row["Collection State"])})
    input_pairs = {(x.brand_original, x.model_original) for x in
                   p.read_input_rows(input_path) if x.valid}
    final_pairs, final_sheet = rows_by_pair(final_path)
    outputs, output_total, output_status = load_results(output_path)
    keys = {p.business_key(x["Marca"], x["Modelo"]) for x in report_rows}
    checkpoints, reservations, runs, db_status = sqlite_history(sqlite_path, keys)
    with closing(p.TavilyReadOnlyStore(cache_path)) as cache:
        rows = classify_reconciliation(
            report=report_rows, input_pairs=input_pairs, final_pairs=final_pairs,
            output_rows=outputs, checkpoint_rows=checkpoints,
            reservations=reservations, cache_reader=cache)
    assert len(rows) == len(report_rows)
    summary = {
        "status": "AUDIT_READ_ONLY",
        "conclusions_provisional": bool(runs and runs[0]["status"] == "RUNNING"),
        "source": {
            "collection": str(report_path), "result": str(output_path),
            "final": str(final_path), "cache": str(cache_path),
            "classification_db": str(sqlite_path),
        },
        "collection_report_rows": len(collection),
        "collection_unique_exact_pairs": len(rows),
        "collection_unique_normalized_keys": len(keys),
        "collection_report_requests": sum(
            int(pd.to_numeric(x["Requests"], errors="coerce")
                if pd.notna(pd.to_numeric(x["Requests"], errors="coerce")) else 0)
            for x in report_rows),
        "classification_result_xlsx_rows": output_total,
        "classification_result_xlsx_status": output_status,
        "classification_checkpoint_pairs_for_report": len(checkpoints),
        "collection_reservations_for_report": len(reservations),
        "sqlite_status": db_status,
        "final_sheet": final_sheet,
        "status_counts": dict(sorted(Counter(
            x["Estado reconciliação"] for x in rows).items())),
        "cache_counts": dict(sorted(Counter(
            x["RAW State atual"] for x in rows).items())),
        "classification_sources": dict(sorted(Counter(
            x["Fonte resultado"] or "SEM_FONTE" for x in rows).items())),
        "latest_runs": runs,
        "search_calls": 0, "jev_calls": 0,
        "official_workbooks_modified": False,
        "notes": [
            "Relatório de coleta pode ser de uma execução diferente da classificação atual.",
            "SQLite contém histórico; uma classificação antiga não comprova a classificação atual.",
            "CLASSIFIED_CLEF é proposta para double-check, não aprovação humana.",
            "Use os pares exatos Marca+Modelo; não subtraia totais de coortes diferentes.",
        ],
    }
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + str(os.getpid())
    csv_path = root / f"reconciliacao_coleta_{stamp}.csv"
    json_path = root / f"reconciliacao_coleta_{stamp}.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary["report_csv"] = str(csv_path)
    summary["report_json"] = str(json_path)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
