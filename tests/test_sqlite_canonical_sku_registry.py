"""Verify one canonical SQLite lookup is merged from official workbook only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from openpyxl import Workbook
from sku_registry_sqlite import audit_sqlite_merge


def write_excel(path, sheetname, rows):
    wb=Workbook()
    ws=wb.active
    ws.title=sheetname
    ws.append(["Marca","Modelo","O que é (Tipo)","Alimentação",
               "Category General","FAMILY","Título Real","Próxima Ação",
               "Status","Revisão","Revisão B.I"])
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()


class SqliteRegistryMergeTests(unittest.TestCase):
    def test_merge_fifty_preserves_human_and_pending_without_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            final,master,db=p/"final.xlsx",p/"master.xlsx",p/"pipeline_v3.sqlite"
            rows=[
                ("FIXTEC","FCD12L06 UL","","","","","","REVISAR","Script","REVISAR",""),
                ("FIXTEC","FCD20401","DRILL","BATTERY","OTHERS","POWER TOOLS",
                 "Cordless drill","DOUBLE_CHECK","Script","PROVÁVEL",""),
                ("FLEX","PE 14 2 150 230/CEE","","","","","","REVISAR","Script","REVISAR",""),
                ("A","HUMAN","ENGINE","PETROL","OTHERS","ENGINE",
                 "Approved","CONCLUIDO","Manual","VALIDADO_MANUAL","OK"),
            ]
            rows+= [("ACME",f"P{i:03d}","","","","","","REVISAR","Script","REVISAR","")
                    for i in range(46)]
            write_excel(final,"Base Final",rows)
            write_excel(master,"Consolidado",rows)
            con=sqlite3.connect(db)
            con.execute("CREATE TABLE classification_results(checkpoint_id TEXT PRIMARY KEY, result_json TEXT)")
            con.execute("INSERT INTO classification_results VALUES ('checkpoint', '{}')")
            con.commit()
            con.close()
            keys={"fixtec|fcd12l06ul", "fixtec|fcd20401",
                  "flex|pe142150230cee","a|human"}
            keys.update(f"acme|p{i:03d}" for i in range(46))
            before=final.read_bytes()
            dry=audit_sqlite_merge(final,master,db,keys)
            self.assertEqual(dry["status"],"DRY_RUN_PASS")
            self.assertEqual(dry["campaign_found_in_final"],50)
            self.assertEqual(dry["unique_final_keys"],50)
            self.assertEqual(before,final.read_bytes())
            done=audit_sqlite_merge(final,master,db,keys,commit=True)
            self.assertEqual(done["status"],"SQLITE_MERGED")
            self.assertEqual(final.read_bytes(),before)
            self.assertEqual(set(p.iterdir()),{final,master,db})
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute(
                    "SELECT count(*) FROM sku_registry_lookup_v1").fetchone()[0],50)
                self.assertEqual(con.execute(
                    "SELECT original_pair FROM v_sku_lookup_safe_v1 WHERE sku_key=?",
                    ("fixtec|fcd12l06ul",)).fetchone()[0], "FIXTEC|FCD12L06 UL")
                self.assertEqual(con.execute(
                    "SELECT classification_state,review_bi FROM sku_registry_lookup_v1 WHERE sku_key='a|human'"
                ).fetchone(),("BI_OK_LOCK","OK"))
                self.assertEqual(con.execute("SELECT count(*) FROM classification_results").fetchone()[0],1)

    def test_refresh_sqlite_projection_after_final_changes(self):
        import pipeline_produtos as pipe
        with tempfile.TemporaryDirectory() as d:
            base=Path(d); final=base/"final.xlsx"; master=base/"master.xlsx"
            db=base/"pipeline_v3.sqlite"
            rows=[("FIXTEC","FCD12L06 UL","","","","","","REVISAR","Script","REVISAR","")]
            write_excel(final,"Base Final",rows)
            write_excel(master,"Consolidado",rows)
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE classification_results(id TEXT PRIMARY KEY)")
                c.execute("INSERT INTO classification_results VALUES ('prior')")
            cfg={"files":{"classification_db":str(db)}}
            first=pipe._refresh_sqlite_sku_registry(master,final,cfg)
            self.assertEqual(first["lookup_pending"],1)
            updated=[("FIXTEC","FCD12L06 UL","DRILL","BATTERY","OTHERS",
                      "POWER TOOLS","Real title","DOUBLE_CHECK","Script","PROVÁVEL","")]
            write_excel(final,"Base Final",updated)
            write_excel(master,"Consolidado",updated)
            second=pipe._refresh_sqlite_sku_registry(
                master,final,cfg,{"fixtec|fcd12l06ul"})
            self.assertEqual(second["status"],"SQLITE_MERGED")
            self.assertEqual(second["campaign_found_in_final"],1)
            self.assertEqual(second["lookup_proposals"],1)
            with sqlite3.connect(db) as c:
                result=c.execute("SELECT product_type, classification_state "
                    "FROM sku_registry_lookup_v1 WHERE sku_key='fixtec|fcd12l06ul'").fetchone()
                self.assertEqual(result,("DRILL","COMPLETE_PROPOSAL"))
                self.assertEqual(c.execute("SELECT COUNT(*) FROM classification_results").fetchone()[0],1)

    def test_alias_collision_is_blocked_on_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);f=p/"final.xlsx";m=p/"master.xlsx";db=p/"pipeline_v3.sqlite"
            rows=[
                ("FIXTEC","FCD12L06 UL","DRILL","BATTERY","OTHERS","DRILLERS",
                 "Title","DOUBLE_CHECK","Script","PROVÁVEL",""),
                ("FIXTEC","FCD12L06UL","ENGINE","PETROL","OTHERS","ENGINE",
                 "Title2","REVISAR","Script","REVISAR",""),
            ]
            write_excel(f,"Base Final",rows)
            write_excel(m,"Consolidado",rows)
            sqlite3.connect(db).close()
            report=audit_sqlite_merge(f,m,db,{"fixtec|fcd12l06ul"},commit=True)
            self.assertEqual(report["status"],"BLOCKED_MISSING_OR_AMBIGUOUS")
            self.assertEqual(report["global_ambiguous"],1)
            with sqlite3.connect(db) as con:
                self.assertFalse(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='sku_registry_lookup_v1'"
                ).fetchone())


if __name__=="__main__":
    unittest.main()
