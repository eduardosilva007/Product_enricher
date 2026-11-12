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


    def test_native_cleanup_preserves_rows_and_human_locks_with_backup(self):
        from openpyxl import Workbook, load_workbook
        from tempfile import TemporaryDirectory
        from pipeline_produtos import clean_final_business_schema, BASELINE_PUBLIC_COLUMNS
        with TemporaryDirectory() as root:
            path=Path(root)/"final.xlsx"
            wb=Workbook()
            ws=wb.active
            ws.title="Base Final"
            ws.append(BASELINE_PUBLIC_COLUMNS+["Status Coleta","Reason Code"])
            ws.append(["DCA","AQF32","BLOWER","CORDED","CORDED PRODUCT",
                       "CORDED BLOWER","DCA AQF32","CONCLUIDO","Script",
                       "REVISAR","", "COLETA_VAZIA", "RULE_UNIQUE"])
            ws.append(["DCA","AQF32","BLOWER","CORDED","CORDED PRODUCT",
                       "CORDED BLOWER","DCA AQF32","CONCLUIDO","Manual",
                       "VALIDADO_MANUAL","OK", "COLETA_VAZIA", "RULE_UNIQUE"])
            wb.save(path)
            wb.close()
            previous=path.read_bytes()
            dry=clean_final_business_schema(path,commit=False)
            self.assertEqual(dry["status"],"DRY_RUN_PASS")
            self.assertEqual(path.read_bytes(),previous)
            report=clean_final_business_schema(path,commit=True)
            self.assertEqual(report["status"],"COMMITTED")
            self.assertEqual(Path(report["backup"]).read_bytes(),previous)
            wb=load_workbook(path,read_only=True)
            ws=wb.active
            self.assertEqual(list(next(ws.values)),BASELINE_PUBLIC_COLUMNS)
            self.assertEqual(ws.max_row,3)
            self.assertEqual(ws.cell(3,10).value,"VALIDADO_MANUAL")
            self.assertEqual(ws.cell(3,11).value,"OK")
            wb.close()

if __name__=="__main__":
    unittest.main()
