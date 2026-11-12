"""Verify that the official final workbook exposes only business columns."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class FinalPublicSchemaTest(unittest.TestCase):
    def test_exact_ordered_public_schema(self):
        tree=ast.parse((ROOT/"pipeline_produtos.py").read_text(encoding="utf-8-sig"))
        values={}
        for n in tree.body:
            if isinstance(n,ast.Assign):
                for target in n.targets:
                    if isinstance(target,ast.Name) and target.id=="BASELINE_PUBLIC_COLUMNS":
                        values[target.id]=ast.literal_eval(n.value)
        self.assertEqual(values["BASELINE_PUBLIC_COLUMNS"],[
            "Marca","Modelo","O que é (Tipo)","Alimentação",
            "Category General","FAMILY","Título Real","Próxima Ação",
            "Status","Revisão","Revisão B.I"])

    def test_final_writer_projects_public_columns(self):
        source=(ROOT/"pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn('out.loc[:, BASELINE_PUBLIC_COLUMNS]',source)
        self.assertIn('sheet_name="Base Final"',source)

if __name__=="__main__":
    unittest.main()
