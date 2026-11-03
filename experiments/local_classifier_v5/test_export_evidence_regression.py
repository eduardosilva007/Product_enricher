#!/usr/bin/env python3
"""No-cost regressions for additive Excel status and evidence export."""
import ast
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pipeline_produtos import _export_base_final, _sanitize_legacy_unresolved_taxonomy, BASELINE_FINAL_COLUMNS

class ExportRegression(unittest.TestCase):
    def test_syntax(self):
        ast.parse((ROOT/"pipeline_produtos.py").read_text(encoding="utf-8"))
    def test_schema_preserves_evidence(self):
        for col in ("Status Coleta","Status Identidade","Status Classificação",
                    "Título Referência","Fonte URL","Fonte Referência URL",
                    "Motivo","Sugestão Category General","Sugestão FAMILY"):
            self.assertIn(col, BASELINE_FINAL_COLUMNS)
    def test_incomplete_candidates_survive_sanitization(self):
        import pandas as pd
        df=pd.DataFrame([{"Marca":"TEST","Modelo":"X1","Status":"Script",
            "Revisão":"REVISAR","O que é (Tipo)":"REVISAR",
            "Category General":"OTHERS","FAMILY":"ANGLE GRINDER"}])
        out,n=_sanitize_legacy_unresolved_taxonomy(df)
        self.assertEqual(n,1)
        self.assertEqual(out.iloc[0]["Sugestão Category General"],"OTHERS")
        self.assertEqual(out.iloc[0]["Sugestão FAMILY"],"ANGLE GRINDER")
        self.assertTrue(pd.isna(out.iloc[0]["Category General"]))
    def test_export_retains_title_url_and_partial_taxonomy(self):
        import pandas as pd
        df=pd.DataFrame([
            {"Marca":"BOSCH","Modelo":"GOP185LI","Status":"Script","Revisão":"REVISAR",
             "O que é (Tipo)":"REVISAR","Alimentação":"BATTERY",
             "Category General":"OTHERS","FAMILY":"OSCILLATING MULTI-TOOL",
             "Título Real":"BOSCH GOP 185 LI multi-cutter","Fonte URL":"https://example.com/gop",
             "Raw State":"RAW_OK","Collection State":"TARGET_EVIDENCE",
             "Classification Identity":"STRONG","Review Required":"SIM",
             "Reason Code":"TEST_PARTIAL","Motivo":"classificacao incompleta"},
            {"Marca":"TEST","Modelo":"N1","Status":"Script","Revisão":"PENDENTE",
             "O que é (Tipo)":"","Título Real":"","Raw State":"RAW_EMPTY"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"final.xlsx"
            result=_export_base_final(df,path,review_bi_snapshot={},review_bi_source_exists=False,
                bi_lock_snapshot={},input_path=None)
            self.assertEqual(result,2)
            out=pd.read_excel(path,dtype=object).set_index("Modelo")
            a=out.loc["GOP185LI"]
            self.assertEqual(a["Título Real"],"BOSCH GOP 185 LI multi-cutter")
            self.assertEqual(a["Fonte URL"],"https://example.com/gop")
            self.assertEqual(a["Sugestão FAMILY"],"OSCILLATING MULTI-TOOL")
            self.assertTrue(pd.isna(a["FAMILY"]))
            self.assertNotEqual(a["Revisão"],"AUTO_APROVADO")
            self.assertEqual(a["Status Identidade"],"MARCA_MODELO_CONFIRMADOS")
            self.assertEqual(out.loc["N1"]["Status Coleta"],"COLETA_VAZIA")
    def test_gold_untouched(self):
        import pandas as pd
        df=pd.DataFrame([{"Marca":"HONDA","Modelo":"GOLD1","Status":"Manual",
            "Revisão":"VALIDADO_MANUAL","Revisão B.I":"OK",
            "O que é (Tipo)":"ENGINE","Alimentação":"PETROL",
            "Category General":"OTHERS","FAMILY":"ENGINE",
            "Título Real":"Honda Engine Gold"}])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"final.xlsx"
            _export_base_final(df,path,review_bi_snapshot={"HONDA|GOLD1":"OK"},
                review_bi_source_exists=False,bi_lock_snapshot={},input_path=None)
            out=pd.read_excel(path,dtype=object).iloc[0]
            for key,value in [("Status","Manual"),("Revisão","VALIDADO_MANUAL"),
                ("Revisão B.I","OK"),("Category General","OTHERS"),("FAMILY","ENGINE")]:
                self.assertEqual(out[key],value)
if __name__=="__main__":unittest.main()
