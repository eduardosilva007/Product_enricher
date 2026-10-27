"""Auditoria histórica SOMENTE LEITURA: resultados antigos ausentes das bases atuais.

Uso: python auditar_historico_classificacao.py
Opcional: --backups (também examina backups do Consolidado/Base Final).
"""
import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import pandas as pd
from pipeline_produtos import business_key, find_col, safe


def read_keys(path):
    if not path.exists():
        return {}
    book = pd.ExcelFile(path)
    try:
        for sheet in (["Base Final", "Consolidado", "Resultados"] +
                      [x for x in book.sheet_names if x not in {"Base Final", "Consolidado", "Resultados"}]):
            if sheet not in book.sheet_names:
                continue
            df = pd.read_excel(book, sheet_name=sheet, dtype=object)
            b = find_col(df, ["Marca", "Fabricante", "Brand", "Manufacturer"], required=False)
            m = find_col(df, ["Modelo", "Model", "SKU", "Part Number"], required=False)
            if not b or not m:
                continue
            out = {}
            for _, row in df.iterrows():
                brand, model = safe(row.get(b)), safe(row.get(m))
                if brand and model:
                    out[business_key(brand, model)] = (brand, model)
            return out
    finally:
        book.close()
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pipeline_v3.sqlite")
    ap.add_argument("--master", default="Consolidado Base de Produtos Enriquecida.xlsx")
    ap.add_argument("--final", default="Base Final Produtos Enriquecida.xlsx")
    ap.add_argument("--backups", action="store_true")
    ap.add_argument("--backup-dir", default="Backups_Baseline")
    ap.add_argument("--output", default="Auditoria_Historico_Classificacao.csv")
    args = ap.parse_args()

    master = read_keys(Path(args.master))
    final = read_keys(Path(args.final))
    history = defaultdict(lambda: {"brands": set(), "models": set(), "dates": [],
                                   "sources": set(), "states": set()})

    db = Path(args.db)
    if db.exists():
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "classification_results" in tables:
                for key, brand, model, updated, raw in conn.execute(
                    "SELECT business_key, brand, model, updated_at, result_json FROM classification_results"
                ):
                    calculated = business_key(safe(brand), safe(model))
                    key = calculated or safe(key)
                    if not key:
                        continue
                    h = history[key]
                    h["brands"].add(safe(brand))
                    h["models"].add(safe(model))
                    h["dates"].append(safe(updated))
                    h["sources"].add("classification_results")
                    try:
                        doc = json.loads(raw)
                        if isinstance(doc, dict):
                            for name in ("Resolution State", "resolution_state", "Review Required", "Revisão"):
                                if name in doc:
                                    h["states"].add(name + "=" + safe(doc[name]))
                    except (ValueError, TypeError):
                        h["states"].add("RESULT_JSON_INVALID")
        finally:
            conn.close()

    if args.backups:
        root = Path(args.backup_dir)
        for path in sorted(root.rglob("*.xlsx")) if root.exists() else []:
            if path.name not in {Path(args.master).name, Path(args.final).name}:
                continue
            try:
                keys = read_keys(path)
                for key, (brand, model) in keys.items():
                    h = history[key]
                    h["brands"].add(brand)
                    h["models"].add(model)
                    h["sources"].add(str(path))
            except Exception as exc:
                print("BACKUP_NAO_LIDO:", path, type(exc).__name__, str(exc))

    rows = []
    for key, h in sorted(history.items()):
        if key in final and key in master:
            continue
        rows.append({
            "business_key": key,
            "Marca histórica": " | ".join(sorted(x for x in h["brands"] if x)),
            "Modelo histórico": " | ".join(sorted(x for x in h["models"] if x)),
            "No Consolidado": "SIM" if key in master else "NAO",
            "Na Base Final": "SIM" if key in final else "NAO",
            "Data mais recente": max(h["dates"]) if h["dates"] else "",
            "Estados históricos": " | ".join(sorted(h["states"])),
            "Fontes históricas": " | ".join(sorted(h["sources"])),
            "Diagnóstico": "AUSENTE_AMBAS" if key not in final and key not in master
                           else "AUSENTE_BASE_FINAL",
        })

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "business_key", "Marca histórica", "Modelo histórico", "No Consolidado",
            "Na Base Final", "Data mais recente", "Estados históricos",
            "Fontes históricas", "Diagnóstico"
        ], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    print("BASE_FINAL_CHAVES:", len(final))
    print("CONSOLIDADO_CHAVES:", len(master))
    print("HISTORICO_CHAVES:", len(history))
    print("HISTORICOS_AUSENTES_BASE_FINAL_OU_CONSOLIDADO:", len(rows))
    print("AUSENTES_NAS_DUAS:", sum(r["Diagnóstico"] == "AUSENTE_AMBAS" for r in rows))
    print("RELATORIO:", args.output)
    print("Nenhuma planilha ou banco existente foi modificado.")


if __name__ == "__main__":
    main()
