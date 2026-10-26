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
