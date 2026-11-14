"""Regression: reconciliation is offline, exact-pair, conservative and read-only."""
from collections import defaultdict
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import pandas as pd

import pipeline_produtos as p
from scripts.audit_full_reconciliation_readonly import (
    main, classify_reconciliation, valid_classification, evidence_ready,
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LocalCache:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, brand, model):
        return self.mapping[(brand, model)]


class ReconciliationLogicTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "Marca": "BRAND", "Modelo": "A1", "Resolution State": "CLASSIFIED_CLEF",
            "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
            "FAMILY": "DRILLERS", "Título Real": "Brand drill A1",
        }

    def test_checkpoint_is_not_human_approval(self):
        self.assertTrue(valid_classification(self.source))
        self.assertFalse(valid_classification({**self.source, "FAMILY": ""}))
        self.assertFalse(valid_classification({**self.source, "Resolution State": "REVIEW"}))

    def test_exact_pair_prevents_alias_inheritance(self):
        key = p.business_key("BRAND", "AB-01")
        normalized_alias = ("BRAND", "AB01")
        self.assertEqual(key, p.business_key(*normalized_alias))
        target = ("BRAND", "AB-01")
        final = {normalized_alias: [(2, dict(self.source), False)]}
        cache = LocalCache({target: type("R", (), {"state": "RAW_OK", "payload": {
            "_collection": {"state": "READY_FOR_CLASSIFICATION",
                            "curated_results": [{"title": "A"}]}}})()})
        result = classify_reconciliation(
            report=[{"Marca": target[0], "Modelo": target[1], "Requests": 1,
                     "RAW Results": 1, "Curated Sources": 1,
                     "Collection State": "READY_FOR_CLASSIFICATION"}],
            input_pairs={target}, final_pairs=final, output_rows={target: [self.source]},
            checkpoint_rows={}, reservations=set(), cache_reader=cache)[0]
        self.assertFalse(result["Par exato na Base Final"])
        self.assertEqual(result["Estado reconciliação"], "CLASSIFICADO_SEM_GRAVACAO_CONFIRMADA")


class ReconciliationIntegrationTests(unittest.TestCase):
    def test_five_sku_end_to_end_no_canonical_writes(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            letters = ["A1", "B2", "C3", "D4", "E5"]
            df_input = pd.DataFrame(
                [{"Marca": "BRAND", "Modelo": x} for x in letters])
            df_input.to_excel(root / "Base de Produtos.xlsx", index=False)
            df_final = pd.DataFrame([
                {"Marca": "BRAND", "Modelo": "A1", "O que é (Tipo)": "DRILL",
                 "Category General": "OTHERS", "FAMILY": "DRILLERS",
                 "Título Real": "Brand drill A1", "Status": "Script",
                 "Revisão": "PROVÁVEL"},
                {"Marca": "BRAND", "Modelo": "B2", "Status": "Script",
                 "Revisão": "PENDENTE"},
                {"Marca": "BRAND", "Modelo": "C3", "Status": "Script",
                 "Revisão": "REVISAR"},
                {"Marca": "BRAND", "Modelo": "D4", "Status": "Manual",
                 "Revisão B.I": "OK", "O que é (Tipo)": "ENGINE",
                 "Category General": "OTHERS", "FAMILY": "ENGINE"},
                {"Marca": "BRAND", "Modelo": "E5", "Status": "Script"},
            ])
            df_final.to_excel(root / "Base Final Produtos Enriquecida.xlsx", index=False)
            collection = pd.DataFrame([
                {"Marca": "BRAND", "Modelo": x, "Provider Requests": 1,
                 "RAW Results": 0 if x == "B2" else 1,
                 "Curated Sources": 0 if x == "B2" else 1,
                 "Collection State": "NO_TARGET_EVIDENCE" if x == "B2"
                                      else "READY_FOR_CLASSIFICATION"}
                for x in letters
            ])
            with pd.ExcelWriter(root / "Coleta.xlsx") as writer:
                collection.to_excel(writer, sheet_name="Produtos", index=False)
            source_a = {"Marca": "BRAND", "Modelo": "A1",
                        "Resolution State": "CLASSIFIED_CLEF",
                        "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
                        "FAMILY": "DRILLERS", "Título Real": "Brand drill A1"}
            with pd.ExcelWriter(root / "Classificacao.xlsx") as writer:
                pd.DataFrame([source_a]).to_excel(writer, sheet_name="Resultados", index=False)
            cache_path = root / "cache_tavily.sqlite"
            writer = p.TavilyWritableStore(cache_path)
            try:
                for x in letters:
                    result = [{"title": f"Brand {x}", "url": "https://example.org",
                               "content": "verified"}] if x != "B2" else []
                    writer.put("BRAND", x, {
                        "results": result,
                        "_collection": {"state": "READY_FOR_CLASSIFICATION",
                                        "curated_results": result},
                    })
            finally:
                writer.close()
            db_path = root / "pipeline_v3.sqlite"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE classification_results (brand TEXT,"
                           "model TEXT,business_key TEXT,result_json TEXT,"
                           "updated_at TEXT,rules_hash TEXT)")
                source_c = dict(source_a, Modelo="C3")
                db.execute("INSERT INTO classification_results VALUES(?,?,?,?,?,?)",
                           ("BRAND", "C3", p.business_key("BRAND", "C3"),
                            json.dumps(source_c), "2026-11-14T00:00:00", "rule"))
                db.commit()
            cfg = {"files": {
                "collection_report": "Coleta.xlsx",
                "input": "Base de Produtos.xlsx",
                "final": "Base Final Produtos Enriquecida.xlsx",
                "cache": "cache_tavily.sqlite",
                "classification_db": "pipeline_v3.sqlite",
                "output": "Classificacao.xlsx",
            }}
            (root / "pipeline_config.json").write_text(json.dumps(cfg), encoding="utf-8")
            canonical = [root / filename for filename in
                         ("Coleta.xlsx", "Base de Produtos.xlsx",
                          "Base Final Produtos Enriquecida.xlsx",
                          "cache_tavily.sqlite", "pipeline_v3.sqlite",
                          "Classificacao.xlsx")]
            before = {x.name: sha(x) for x in canonical}
            with redirect_stdout(io.StringIO()):
                code = main(["--config", str(root / "pipeline_config.json"),
                             "--output-dir", str(root / "diagnostics")])
            self.assertEqual(code, 0)
            self.assertEqual(before, {x.name: sha(x) for x in canonical})
            reports = list((root / "diagnostics").glob("*.json"))
            self.assertEqual(len(reports), 1)
            summary = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(summary["collection_unique_exact_pairs"], 5)
            self.assertEqual(summary["classification_result_xlsx_rows"], 1)
            self.assertEqual(summary["classification_checkpoint_pairs_for_report"], 1)
            self.assertEqual(summary["status_counts"], {
                "BLOQUEADO_POR_REVISAO_HUMANA": 1,
                "CLASSIFICADO_E_GRAVADO_CONFERIDO": 1,
                "CLASSIFICADO_SEM_GRAVACAO_CONFIRMADA": 1,
                "ELEGIVEL_SEM_RESULTADO_GRAVADO": 1,
                "RAW_INDISPONIVEL": 1,
            })
            self.assertEqual(summary["official_workbooks_modified"], False)


if __name__ == "__main__":
    unittest.main()
