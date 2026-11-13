#!/usr/bin/env python3
"""Read-only integrity and classification audit for an isolated nightly campaign.

This is NOT a taxonomy approval. It exposes every SKU, including those without
classification output, and never modifies canonical cache, databases or Excel.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline_produtos import business_key, safe  # single authoritative identity key


FIELDS = (
    "Marca", "Modelo", "O que é (Tipo)", "Alimentação", "Category General",
    "FAMILY", "Título Real", "Fonte URL", "Classification Identity",
    "Resolution State", "Classification Outcome", "Review Required",
    "Auto Eligible", "Reason Code", "Motivo", "Próxima Ação",
)
REVIEW_FIELDS = ("Gold O que é (Tipo)", "Gold Category General", "Gold FAMILY",
                 "Auditoria Humana", "Observações do Auditor")
ALLOWED_RESOLUTION = {"RESOLVED", "CLASSIFIED_CLEF", "REVIEW_CACHE_EVIDENCE"}


def read_json(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return obj


def rows_from_xlsx(path: Path, sheet: str | None = None) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        it = ws.iter_rows(values_only=True)
        head = next(it, ())
        names = [safe(x) for x in head]
        if not {"Marca", "Modelo"}.issubset(names) or len(names) != len(set(names)):
            raise ValueError(f"Missing or duplicated Marca/Modelo headers: {path}")
        return [dict(zip(names, values)) for values in it
                if any(safe(v) for v in values)]
    finally:
        wb.close()


def audit(campaign: Path, expected: int = 50) -> tuple[dict, list[dict]]:
    campaign = campaign.resolve()
    state = read_json(campaign / "campaign.json")
    errors: list[str] = []
    all_rows: list[dict] = []
    keys_seen: set[str] = set()
    batches = state.get("batches") or []
    counts = Counter()
    if state.get("status") != "COMPLETE":
        errors.append(f"CAMPAIGN_NOT_COMPLETE:{state.get('status')}")
    if state.get("active_run") or state.get("pending_run_creation"):
        errors.append("UNFINISHED_SUBLOT")
    if state.get("promotion", {}).get("status") == "COMMITTED":
        errors.append("ALREADY_PROMOTED_BEFORE_INDEPENDENT_AUDIT")
    if not state.get("review_before_promotion", False):
        errors.append("HUMAN_REVIEW_FLAG_NOT_ENABLED")
    if state.get("audited_products") != expected:
        errors.append("AUDITED_PRODUCT_COUNT_MISMATCH")
    if state.get("limits", {}).get("target") != expected:
        errors.append("CAMPAIGN_TARGET_MISMATCH")
    if expected == 50 and [b.get("cohort_count") for b in batches] != [20, 20, 10]:
        errors.append("UNEXPECTED_BATCH_SPLIT")

    for index, batch in enumerate(batches, 1):
        run = Path(batch.get("run_dir") or "").resolve()
        if run.parent != campaign.parent.parent:
            errors.append(f"BATCH_{index}_INVALID_PATH")
            continue
        try:
            report = read_json(run / "run_report.json")
            quality = read_json(run / "quality_audit.json")
            isolated = read_json(run / "isolated_config.json")
            cohort = rows_from_xlsx(Path(isolated["files"]["input"]))
            result = rows_from_xlsx(Path(isolated["files"]["output"]), "Resultados")
        except (OSError, ValueError, KeyError, IndexError) as exc:
            errors.append(f"BATCH_{index}_INPUT_OR_OUTPUT_ERROR:{type(exc).__name__}:{exc}")
            continue
        expected_keys = list(batch.get("cohort_keys") or [])
        parsed_keys = [business_key(safe(row["Marca"]), safe(row["Modelo"])) for row in cohort]
        actual_keys = [business_key(safe(row["Marca"]), safe(row["Modelo"])) for row in result]
        if (batch.get("status") != "AUDIT_PASS"
                or report.get("cohort_keys") != expected_keys
                or len(expected_keys) != len(set(expected_keys))
                or parsed_keys != expected_keys
                or batch.get("cohort_count") != len(cohort)
                or report.get("cohort_count") != len(cohort)):
            errors.append(f"BATCH_{index}_COHORT_INTEGRITY")
        if set(keys_seen).intersection(parsed_keys):
            errors.append(f"BATCH_{index}_DUPLICATE_SKU_ACROSS_BATCHES")
        keys_seen.update(parsed_keys)
        if (quality.get("status") != "PASS"
                or quality.get("useful_raw_count") != len(cohort)
                or quality.get("protected_changes_total") != 0
                or quality.get("original_source_modified")
                or report.get("stages", {}).get("collection", {}).get("status") != "PASS"
                or report.get("stages", {}).get("classification", {}).get("status") != "PASS"
                or not report.get("all_stages_pass")):
            errors.append(f"BATCH_{index}_RUN_QUALITY_NOT_PASS")
        raw_states = quality.get("cache_states_after") or {}
        if sum(raw_states.get(s, 0) for s in ("RAW_OK", "RAW_OK_MERGED")) != len(cohort):
            errors.append(f"BATCH_{index}_RAW_COVERAGE_MISMATCH")
        if len(actual_keys) != len(set(actual_keys)) or not set(actual_keys).issubset(set(parsed_keys)):
            errors.append(f"BATCH_{index}_OUTPUT_DUPLICATE_OR_FOREIGN_SKU")
        by_key = {business_key(safe(r["Marca"]), safe(r["Modelo"])): r for r in result}
        for row in cohort:
            brand, model = safe(row["Marca"]), safe(row["Modelo"])
            key = business_key(brand, model)
            found = by_key.get(key)
            record = {field: safe(found.get(field)) if found else "" for field in FIELDS}
            record["Marca"], record["Modelo"] = brand, model
            record["Lote"], record["Business Key"] = index, key
            record["Coleta RAW"] = "RAW_OK"  # corroborated by the audited cohort check
            record["Saída de Classificação"] = "SIM" if found else "NÃO"
            resolution = record["Resolution State"].upper()
            review = record["Review Required"].upper()
            auto = record["Auto Eligible"].upper()
            if not found:
                status = "SEM_SAIDA_DE_CLASSIFICACAO"
            elif resolution not in ALLOWED_RESOLUTION:
                status = "ESTADO_CLASSIFICACAO_INVALIDO"
                errors.append(f"INVALID_RESOLUTION:{key}:{resolution}")
            elif auto == "SIM" and (resolution != "RESOLVED" or review != "NÃO"
                                   or not record["Category General"] or not record["FAMILY"]
                                   or record["Classification Identity"].upper() != "STRONG"):
                status = "FALSA_APROVACAO_AUTOMATICA"
                errors.append(f"FALSE_AUTO_APPROVAL:{key}")
            elif resolution == "RESOLVED" and (auto != "SIM" or review != "NÃO"):
                status = "RESOLVED_INCONSISTENTE"
                errors.append(f"RESOLVED_CONTRACT:{key}")
            elif resolution == "CLASSIFIED_CLEF" and review != "SIM":
                status = "REVISAO_IA_AUSENTE"
                errors.append(f"MISSING_LLM_REVIEW:{key}")
            elif resolution == "REVIEW_CACHE_EVIDENCE" and review != "SIM":
                status = "REVISAO_EVIDENCIA_AUSENTE"
                errors.append(f"MISSING_EVIDENCE_REVIEW:{key}")
            elif resolution == "RESOLVED":
                status = "AUTO_APROVADO_PARA_AUDITORIA"
            else:
                status = "REVISAR_COM_EVIDENCIA"
            record["Status de Auditoria"] = status
            record.update({field: "" for field in REVIEW_FIELDS})
            all_rows.append(record)
            counts[status] += 1

    if len(keys_seen) != expected or len(all_rows) != expected:
        errors.append("GLOBAL_50_SKU_COVERAGE_MISMATCH")
    if len(all_rows) != len(set(r["Business Key"] for r in all_rows)):
        errors.append("GLOBAL_DUPLICATE_SKU")
    status = ("BLOCKED_INTEGRITY_FAILURE" if errors else
              "BLOCKED_CLASSIFICATION_GAPS" if counts["SEM_SAIDA_DE_CLASSIFICACAO"] else
              "PASS_INTEGRITY_REVIEW_PENDING")
    summary = {
        "status": status,
        "expected_skus": expected,
        "audited_skus": len(all_rows),
        "distinct_keys": len(keys_seen),
        "batches": len(batches),
        "classification_results": sum(r["Saída de Classificação"] == "SIM" for r in all_rows),
        "missing_classification_results": counts["SEM_SAIDA_DE_CLASSIFICACAO"],
        "automatic_proposals": counts["AUTO_APROVADO_PARA_AUDITORIA"],
        "llm_or_evidence_review": counts["REVISAR_COM_EVIDENCIA"],
        "status_counts": dict(sorted(counts.items())),
        "errors": errors,
        "human_taxonomy_approved": False,
        "canonical_promotion_permitted": False,
        "search_calls": 0,
        "llm_calls": 0,
    }
    return summary, all_rows


def export(campaign: Path, summary: dict, rows: list[dict]) -> tuple[Path, Path]:
    target = campaign.resolve()
    json_file = target / "audit_50_integrity.json"
    xlsx_file = target / "audit_50_classifications.xlsx"
    json_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    book = Workbook()
    ws = book.active
    ws.title = "Auditoria 50"
    headers = ("Lote", "Business Key", "Coleta RAW", "Saída de Classificação",
               "Status de Auditoria") + FIELDS + REVIEW_FIELDS
    ws.append(headers)
    for record in rows:
        ws.append([record.get(k, "") for k in headers])
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    book.save(xlsx_file)
    return json_file, xlsx_file


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-dir", type=Path, required=True)
    p.add_argument("--expected", type=int, default=50)
    a = p.parse_args()
    if a.expected < 1:
        p.error("--expected must be positive")
    summary, rows = audit(a.campaign_dir, a.expected)
    out = export(a.campaign_dir, summary, rows)
    print(json.dumps({"audit": summary, "outputs": [str(x) for x in out]},
                     ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS_INTEGRITY_REVIEW_PENDING" else 2


if __name__ == "__main__":
    raise SystemExit(main())
