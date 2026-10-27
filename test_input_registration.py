"""Coverage contract: input products must exist in final baseline even if no search runs."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import pipeline_produtos as p

class InputRegistrationTests(unittest.TestCase):
    def test_register_missing_input_and_preserve_human_rows(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp = folder / "Base de Produtos.xlsx"
            pd.DataFrame([
                {"Marca":"BOMTEX", "Modelo":"TEX.0131"},
                {"Marca":"BOMTEX", "Modelo":"TEX.0131"},
                {"Marca":"BRANIK", "Modelo":"PD15RSE"},
                {"Marca":"LOCKED", "Modelo":"OK1"},
            ]).to_excel(inp, index=False)
            master = pd.DataFrame([{
                "Marca":"LOCKED", "Modelo":"OK1", "Status":"Manual",
                "Revisão B.I":"OK", "O que é (Tipo)":"GENERATOR",
                "Category General":"GENERATORS", "FAMILY":"GENERATOR",
            }])
            output, added = p._register_input_keys_in_master(master, inp)
            self.assertEqual(added, 2)
            self.assertEqual(len(output), 3)
            locked = output.loc[output["Modelo"].eq("OK1")].iloc[0]
            self.assertEqual(locked["Revisão B.I"], "OK")
            self.assertEqual(locked["FAMILY"], "GENERATOR")
            pending = output.loc[output["Modelo"].eq("TEX.0131")].iloc[0]
            self.assertEqual(pending["Revisão"], "PENDENTE_COLETA")
            self.assertEqual(pending["Próxima Ação"], "REVISAR_MANUAL_OU_CLASSIFICAR")
            self.assertFalse(bool(p.safe(pending["FAMILY"])))
            twice, second_added = p._register_input_keys_in_master(output, inp)
            self.assertEqual(second_added, 0)
            self.assertEqual(len(twice), len(output))

    def test_bi_ok_approved_row_not_broadcast_to_other_sku_variant(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp, master, final = (folder / n for n in ("entrada.xlsx", "consolidado.xlsx", "final.xlsx"))
            pd.DataFrame([{"Marca":"3D", "Modelo":"L 37 PRO"}]).to_excel(inp, index=False)
            pd.DataFrame([
                {"Marca":"3D", "Modelo":"L 37 PRO", "Revisão B.I":"", "Status":"Script"}
            ]).to_excel(master, sheet_name="Consolidado", index=False)
            pd.DataFrame([{
                "Marca":"3D", "Modelo":"L37PRO",
                "Revisão B.I":"OK", "Revisão":"AUTO_APROVADO",
                "Status":"Script", "O que é (Tipo)":"POLISHER",
                "Alimentação":"UNKNOWN", "Category General":"OTHERS",
                "FAMILY":"POLISHER", "Título Real":"Pulidora Dual Action",
            }]).to_excel(final, sheet_name="Base Final", index=False)
            rules = p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            p.persist_baseline_excels([], master, final, inp, rules)
            result, _ = p._read_business_sheet(final)
            approved = result[result["Modelo"] == "L37PRO"].iloc[0]
            variant = result[result["Modelo"] == "L 37 PRO"].iloc[0]
            self.assertEqual(approved["Revisão B.I"], "OK")
            self.assertEqual(approved["FAMILY"], "POLISHER")
            self.assertNotEqual(p.safe(variant["Revisão B.I"]), "OK")
            self.assertEqual(variant["Revisão"], "PROVÁVEL")
            self.assertEqual(variant["FAMILY"], "POLISHER")
            self.assertEqual(variant["Próxima Ação"], "VALIDAR_EQUIVALENCIA_MODELO")

    def test_exact_model_variants_are_visible_for_procv(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp = folder / "entrada.xlsx"
            master = folder / "consolidado.xlsx"
            final = folder / "final.xlsx"
            pd.DataFrame([
                {"Marca": "TOTAL", "Modelo": "TS11218576/4"},
                {"Marca": "TOTAL", "Modelo": "TS11218576 4"},
                {"Marca": "TOTAL", "Modelo": "TS11218576/4"},
            ]).to_excel(inp, index=False)
            rules = p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            p.persist_baseline_excels([], master, final, inp, rules)
            exported, _ = p._read_business_sheet(final)
            self.assertEqual(set(exported["Modelo"]), {"TS11218576/4", "TS11218576 4"})
            alias = exported.loc[exported["Modelo"].eq("TS11218576 4")].iloc[0]
            self.assertEqual(alias["Revisão"], "PENDENTE")
            self.assertNotEqual(p.safe(alias["Revisão B.I"]), "OK")
            p.persist_baseline_excels([], master, final, inp, rules)
            exported_twice, _ = p._read_business_sheet(final)
            self.assertEqual(len(exported_twice), 2)

    def test_stihl_variants_are_filled_and_preserved_across_syncs(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp, master, final = (folder / x for x in ("input.xlsx", "master.xlsx", "final.xlsx"))
            pd.DataFrame([
                {"Marca":"STIHL","Modelo":"FS 161 L"},
                {"Marca":"STIHL","Modelo":"FS 161/L"},
                {"Marca":"STIHL","Modelo":"FS 161L"},
            ]).to_excel(inp, index=False)
            pd.DataFrame([{
                "Marca":"STIHL","Modelo":"FS 161 L",
                "O que é (Tipo)":"TRIMMER","Alimentação":"PETROL",
                "Category General":"PETROL HANDHELD","FAMILY":"PETROL TRIMMER",
                "Título Real":"FS 161 L petrol brushcutter",
                "Status":"Script","Revisão":"AUTO_APROVADO","Revisão B.I":"OK"
            }]).to_excel(master, sheet_name="Consolidado", index=False)
            # Existing empty variant must be repaired, not skipped.
            pd.DataFrame([
                {"Marca":"STIHL","Modelo":"FS 161 L",
                 "O que é (Tipo)":"TRIMMER","Alimentação":"PETROL",
                 "Category General":"PETROL HANDHELD","FAMILY":"PETROL TRIMMER",
                 "Título Real":"FS 161 L petrol brushcutter",
                 "Status":"Script","Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
                {"Marca":"STIHL","Modelo":"FS 161/L","Status":"Script",
                 "Revisão":"PENDENTE","Próxima Ação":"REVISAR_MANUAL_VARIANTE_CODIGO"},
            ]).to_excel(final, sheet_name="Base Final", index=False)
            rules = p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            for _ in range(2):
                p.persist_baseline_excels([], master, final, inp, rules)
                actual, _ = p._read_business_sheet(final)
                self.assertEqual(set(actual["Modelo"]), {"FS 161 L","FS 161/L","FS 161L"})
                self.assertEqual(len(actual), 3)
                for _, row in actual.iterrows():
                    self.assertEqual(row["FAMILY"], "PETROL TRIMMER")
                    self.assertEqual(row["O que é (Tipo)"], "TRIMMER")
                    if row["Modelo"] != "FS 161 L":
                        self.assertNotEqual(p.safe(row["Revisão B.I"]), "OK")
                        self.assertEqual(row["Revisão"], "PROVÁVEL")

    def test_persist_populates_final_without_classification(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp = folder / "entrada.xlsx"
            master = folder / "consolidado.xlsx"
            final = folder / "final.xlsx"
            pd.DataFrame([{"Marca":"BESTOOL","Modelo":"HL2002"}]).to_excel(inp,index=False)
            rules = p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            result = p.persist_baseline_excels([], master, final, inp, rules)
            self.assertEqual(result["input_registered"], 1)
            exported, _ = p._read_business_sheet(final)
            self.assertEqual(exported["Modelo"].tolist(), ["HL2002"])
            self.assertEqual(exported["Revisão"].iloc[0], "PENDENTE_COLETA")
            again = p.persist_baseline_excels([], master, final, inp, rules)
            self.assertEqual(again["input_registered"], 0)

if __name__ == "__main__":
    unittest.main()
