"""Process-crash/reentry stress on the EXACT campaign worker wrapper and real DurableRouter.

No HTTP calls, no external APIs. Validates the persisted reservation ledger.
"""
from __future__ import annotations
import json,sqlite3,sys,tempfile,textwrap,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from run_controlled_night_pilot import run_stage

class CrashRecoveryTests(unittest.TestCase):
    def test_real_worker_crash_then_resume_same_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="night_crash_") as work:
            d=Path(work)
            db=d/"reservations.sqlite"
            code=d/"worker.py"
            code.write_text(textwrap.dedent("""
                import os,sys
                from pathlib import Path
                sys.path.insert(0,sys.argv[1])
                from nightly_provider_router import DurableRouter,Slot
                p=Path(sys.argv[2])
                slot=Slot("tavily","tavily:test","TAVILY_API_KEY","",1,"test")
                router=DurableRouter(p,[slot])
                if router.already_reserved("CNH|47507106"):
                    assert router.reserve("CNH|47507106") is None
                    router.close()
                    sys.exit(0)
                assert router.reserve("CNH|47507106") is not None
                router.close()
                os._exit(87)
            """),encoding="utf-8")
            state={"stages":{}}
            cmd=[sys.executable,str(code),str(ROOT),str(db)]
            self.assertFalse(run_stage(d,state,"collection",cmd,1))
            self.assertEqual(state["stages"]["collection"]["exit_code"],87)
            self.assertTrue(run_stage(d,state,"collection",cmd,1))
            self.assertEqual(state["stages"]["collection"]["attempts"],2)
            with sqlite3.connect(db) as conn:
                total=conn.execute(
                    "SELECT COUNT(*) FROM collection_attempt_reservations"
                ).fetchone()[0]
            self.assertEqual(total,1)

if __name__=="__main__":unittest.main()
