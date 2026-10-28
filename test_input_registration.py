"""Coverage contract: input products must exist in final baseline even if no search runs."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import pipeline_produtos as p

class InputRegistrationTests(unittest.TestCase):
    def test_rate_limiter_concurrent_slots_are_spaced(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        limiter = p.RateLimiter(1200)  # 50 ms entre slots
        def take_slot(_):
            limiter.wait()
            return time.monotonic()
        with ThreadPoolExecutor(max_workers=6) as pool:
            instants = sorted(pool.map(take_slot, range(6)))
        gaps = [b - a for a, b in zip(instants, instants[1:])]
        self.assertTrue(all(g >= 0.035 for g in gaps), gaps)

    def test_cohort_counts_complete_clef_candidates_without_bi_approval(self):
        key = p.business_key("MAKITA", "HG6030")
        rec = {
            "Marca": "MAKITA", "Modelo": "HG6030",
            "Resolution State": "CLASSIFIED_CLEF",
            "O que é (Tipo)": "HEAT GUN",
            "Category General": "OTHERS", "FAMILY": "HEAT GUN",
            "Review Required": "SIM",
        }
        got = p._expected_cohort_accepted_after_run({key}, set(), [rec])
        self.assertEqual(got, {key})
        rec["FAMILY"] = ""
        self.assertEqual(p._expected_cohort_accepted_after_run({key}, set(), [rec]), set())

    def test_safe_missing_values_do_not_create_fake_classifications(self):
        self.assertEqual(p.safe(pd.NA), "")
        self.assertEqual(p.safe(float("nan")), "")
        self.assertEqual(p.safe(None), "")
        self.assertEqual(p.safe(0), "0")
        self.assertEqual(p.safe(False), "False")

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
            self.assertEqual(variant["Revisão"], "AUTO_APROVADO")
            self.assertEqual(variant["FAMILY"], "POLISHER")
            self.assertEqual(variant["Próxima Ação"], "CONCLUIDO_POR_EQUIVALENCIA_BI")

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
                        self.assertEqual(row["Revisão"], "AUTO_APROVADO")

    def test_conflicting_taxonomy_blocks_bi_inheritance(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp, master, final = (folder / n for n in ("entrada.xlsx", "consolidado.xlsx", "final.xlsx"))
            pd.DataFrame([{"Marca":"TEST","Modelo":"AB/12"}]).to_excel(inp,index=False)
            pd.DataFrame([{
                "Marca":"TEST","Modelo":"AB12","O que é (Tipo)":"DRILL",
                "Category General":"OTHERS","FAMILY":"DRILL",
                "Status":"Script","Revisão":"AUTO_APROVADO","Revisão B.I":"OK",
            }]).to_excel(master,index=False)
            pd.DataFrame([
                {"Marca":"TEST","Modelo":"AB12","O que é (Tipo)":"DRILL",
                 "Category General":"OTHERS","FAMILY":"DRILL",
                 "Status":"Script","Revisão":"AUTO_APROVADO","Revisão B.I":"OK"},
                {"Marca":"TEST","Modelo":"AB/12","O que é (Tipo)":"SAW",
                 "Category General":"OTHERS","FAMILY":"SAW",
                 "Status":"Script","Revisão":"PROVÁVEL"},
            ]).to_excel(final,index=False)
            rules=p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            p.persist_baseline_excels([],master,final,inp,rules)
            exported,_=p._read_business_sheet(final)
            variant=exported.loc[exported["Modelo"].eq("AB/12")].iloc[0]
            self.assertNotEqual(p.safe(variant["Revisão B.I"]),"OK")
            self.assertNotEqual(variant["Próxima Ação"],"CONCLUIDO_POR_EQUIVALENCIA_BI")

    def test_total_variants_share_one_probable_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp, master, final = (folder / n for n in ("entrada.xlsx", "consolidado.xlsx", "final.xlsx"))
            pd.DataFrame([
                {"Marca":"TOTAL","Modelo":"TB20078/4"},
                {"Marca":"TOTAL","Modelo":"TB20078_4"}
            ]).to_excel(inp,index=False)
            pd.DataFrame([{
                "Marca":"TOTAL","Modelo":"TB20078/4",
                "O que é (Tipo)":"HEAT GUN","Alimentação":"CORDED",
                "Category General":"OTHERS","FAMILY":"HEAT GUN",
                "Título Real":"TOTAL Heat gun 2.000W (TB20078)",
                "Status":"Script","Revisão":"PROVÁVEL",
                "Próxima Ação":"DOUBLE_CHECK",
            }]).to_excel(master,index=False)
            rules=p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            for _ in range(2):
                p.persist_baseline_excels([],master,final,inp,rules)
                exported,_=p._read_business_sheet(final)
                self.assertEqual(len(exported),2)
                variant=exported.loc[exported["Modelo"].eq("TB20078_4")].iloc[0]
                self.assertEqual(variant["FAMILY"],"HEAT GUN")
                self.assertEqual(variant["Revisão"],"PROVÁVEL")
                self.assertEqual(variant["Próxima Ação"],"AGUARDAR_REVISAO_PRODUTO_CANONICO")
                self.assertEqual(sum(exported["Próxima Ação"].eq("VALIDAR_EQUIVALENCIA_MODELO")),0)

    def test_technical_auto_approval_propagates_without_bi(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            inp, master, final = (folder / n for n in ("entrada.xlsx", "consolidado.xlsx", "final.xlsx"))
            pd.DataFrame([{"Marca":"TOTAL","Modelo":"TB20078_4"}]).to_excel(inp,index=False)
            pd.DataFrame([{
                "Marca":"TOTAL","Modelo":"TB20078/4",
                "O que é (Tipo)":"HEAT GUN","Alimentação":"CORDED",
                "Category General":"OTHERS","FAMILY":"HEAT GUN",
                "Status":"Script","Revisão":"AUTO_APROVADO","Próxima Ação":"CONCLUIDO",
            }]).to_excel(master,index=False)
            rules=p.Rules(Path(__file__).resolve().parent / "regras_negocio.json")
            p.persist_baseline_excels([],master,final,inp,rules)
            exported,_=p._read_business_sheet(final)
            variant=exported.loc[exported["Modelo"].eq("TB20078_4")].iloc[0]
            self.assertEqual(variant["Revisão"],"AUTO_APROVADO")
            self.assertEqual(variant["Próxima Ação"],"CONCLUIDO_POR_EQUIVALENCIA_TECNICA")
            self.assertNotEqual(p.safe(variant["Revisão B.I"]),"OK")

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
