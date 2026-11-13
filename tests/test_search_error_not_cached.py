"""Operational API failures must never poison RAW; ledger blocks requery."""
from __future__ import annotations
import json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from migrate_legacy_search_errors import is_error_only,migrate

class OperationalFailureIsolation(unittest.TestCase):
    def test_legacy_error_detection_does_not_delete_real_raw(self):
        err={"results":[],"_collection":{"state":"REQUEST_ATTEMPTED_NO_AUTO_RETRY",
             "provider":"serper","error":"SERPER HTTP 400: Not enough credits"}}
        good={"results":[{"title":"CNH 123 FAN"}]}
        truly_empty={"results":[],"_collection":{"state":"NO_TARGET_EVIDENCE"}}
        self.assertTrue(is_error_only(json.dumps(err)))
        self.assertFalse(is_error_only(json.dumps(good)))
        self.assertFalse(is_error_only(json.dumps(truly_empty)))
    def test_error_migration_reserves_before_removing_tombstone(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            cache=root/"cache.sqlite";ledger=root/"class.sqlite"
            error=json.dumps({"results":[],"_collection":{"state":"REQUEST_ATTEMPTED_NO_AUTO_RETRY",
                                  "provider":"serper","error":"Not enough credits"}})
            valid=json.dumps({"results":[{"title":"CNH 2 fan"}]})
            with closing(sqlite3.connect(cache)) as db:
                db.execute("""CREATE TABLE tavily_cache (cache_key TEXT PRIMARY KEY,
                           brand TEXT,model TEXT,response_json TEXT, created_at TEXT)""")
                db.executemany("INSERT INTO tavily_cache VALUES(?,?,?,?,?)",[
                    ("a","CNH","1",error,"today"),("b","CNH","2",valid,"today")])
                db.commit()
            dry=migrate(cache,ledger,False,root/"never")
            self.assertEqual(dry["error_only"],1)
            self.assertFalse(ledger.exists())
            done=migrate(cache,ledger,True,root/"backups")
            self.assertEqual(done["removed_legacy_error_cache_rows"],1)
            self.assertTrue((root/"backups"/"cache_before.sqlite").is_file())
            with closing(sqlite3.connect(cache)) as db:
                self.assertEqual(db.execute("SELECT cache_key FROM tavily_cache").fetchall(),[("b",)])
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual(db.execute("SELECT business_key FROM collection_attempt_reservations").fetchall(),
                                 [("cnh|1",)])
    def test_production_writer_does_not_store_error_tombstones(self):
        code=(ROOT/"pipeline_produtos.py").read_text(encoding="utf-8")
        self.assertNotIn("writer.put(brand, model, failure",code)
        self.assertIn('SEARCH_ERROR_NOT_CACHED',code)
        self.assertIn('INSERT OR IGNORE INTO collection_attempt_reservations',code)
        self.assertIn('exclude_attempted_keys=attempted_keys',code)

if __name__=="__main__":unittest.main()
