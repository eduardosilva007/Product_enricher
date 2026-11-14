"""Offline integration/guard tests for the paid 50-SKU FULL pilot and no-API replay."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

import pipeline_produtos as p
from scripts.pilot_50_full_gate import prepare, execute
from scripts.replay_locked_classification_offline import read_result, run
from tools.verify_pilot_50_release import check as verify_mass_gate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_fixture(root, *, input_count=2):
    root = Path(root)
    master = root / "Consolidado Base de Produtos Enriquecida.xlsx"
    final = root / "Base Final Produtos Enriquecida.xlsx"
    cache = root / "cache_tavily.sqlite"
    db = root / "pipeline_v3.sqlite"
    input_path = root / "Base de Produtos.xlsx"
    result = root / "Classificacao_v3.xlsx"
    human = {"Marca":"HUMAN", "Modelo":"LOCK-01", "Status":"Manual",
             "Revisão B.I":"OK", "Revisão":"VALIDADO_MANUAL",
             "O que é (Tipo)":"ENGINE", "Alimentação":"PETROL",
             "Category General":"OTHERS", "FAMILY":"ENGINE",
             "Título Real":"Human-approved engine"}
    pd.DataFrame([human]).to_excel(master,index=False)
    pd.DataFrame([human]).to_excel(final,sheet_name="Base Final",index=False)
    pd.DataFrame([{"Marca":"BRAND","Modelo":f"SKU-{i:05d}"}
                  for i in range(input_count)]).to_excel(input_path,index=False)
    w=p.TavilyWritableStore(cache)
    w.close()
    d=p.ClassificationDB(db)
    d.close()
    config = {"files":{
        "input":input_path.name,"master":master.name,"final":final.name,
        "cache":cache.name,"classification_db":db.name,
        "output":result.name,
        "collection_report":"Coleta.xlsx",
        "collection_policy":"collection_quality_policy.json",
    }}
    cfg_file = root / "pipeline_config.json"
    cfg_file.write_text(json.dumps(config),encoding="utf-8")
    return cfg_file, master, final, cache, db, input_path, result


class OfflineReplayTests(unittest.TestCase):
    def test_read_result_rejects_wrong_cohort_or_nonlocked_failure(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result = local_fixture(d)
            rules=p.Rules(Path(p.__file__).with_name("regras_negocio.json"))
            rows=[{"Marca":"BRAND","Modelo":"SKU-00000",
                   "Resolution State":"RESOLVED",
                   "Review Required":"NÃO","Classification Identity":"STRONG",
                   "O que é (Tipo)":"DRILL", "Category General":"OTHERS",
                   "FAMILY":"DRILLERS", "Título Real":"Brand drill"},
                  {"Marca":"BRAND","Modelo":"SKU-00001",
                   "Resolution State":"CLASSIFIED_CLEF","Review Required":"SIM",
                   "Próxima Ação":"DOUBLE_CHECK",
                   "Classification Identity":"PROBABLE",
                   "O que é (Tipo)":"DRILL", "Category General":"OTHERS",
                   "FAMILY":"DRILLERS", "Título Real":"Brand drill 2"}]
            metrics={"cohort_size":2,"Rules Hash":rules.hash,
                     "Pipeline Version":p.PIPELINE_VERSION,
                     "Baseline Warning":"PermissionError: BASELINE_FILE_LOCKED"}
            with pd.ExcelWriter(result) as writer:
                pd.DataFrame(rows).to_excel(writer,sheet_name="Resultados",index=False)
                pd.DataFrame([{"Métrica":k,"Valor":v} for k,v in metrics.items()]).to_excel(
                    writer,sheet_name="Resumo",index=False)
            parsed, keys = read_result(result,2,rules)
            self.assertEqual(len(parsed),2)
            self.assertEqual(len(keys),2)
            with self.assertRaisesRegex(RuntimeError,"RESULT_ROW_COUNT"):
                read_result(result,338,rules)

            args=argparse.Namespace(config=str(cfg), rules=str(Path(p.__file__).with_name(
                "regras_negocio.json")), result=str(result),expected_rows=2,commit=False)
            before={x.name:sha(x) for x in (master,final,cache,db,result)}
            with redirect_stdout(io.StringIO()):
                status=run(args)
            self.assertEqual(status,0)
            self.assertEqual(before,{x.name:sha(x) for x in (master,final,cache,db,result)})

    def test_excel_warning_missing_can_be_proven_by_exact_failed_sqlite_run(self):
        import sqlite3
        from datetime import datetime, timezone, timedelta

        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result = local_fixture(d)
            rules = p.Rules(Path(p.__file__).with_name("regras_negocio.json"))
            records = [
                {"Marca": "BRAND", "Modelo": f"SKU-{i:05d}",
                 "Resolution State": "RESOLVED",
                 "Review Required": "NÃO", "Classification Identity": "STRONG",
                 "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
                 "FAMILY": "DRILLERS", "Título Real": f"Brand drill {i}",
                 "Próxima Ação": "CONCLUIDO"}
                for i in range(4)
            ]
            keylines = "\n".join(
                p.business_key(x["Marca"], x["Modelo"]) for x in records)
            cohort_hash = hashlib.sha256(keylines.encode("utf-8")).hexdigest()[:12]
            summary = {
                "cohort_size": 4,
                "Pilot Cohort Hash": cohort_hash,
                "Rules Hash": rules.hash,
                "Pipeline Version": p.PIPELINE_VERSION,
                "Baseline Warning": "",
            }
            with pd.ExcelWriter(result) as writer:
                pd.DataFrame(records).to_excel(writer, sheet_name="Resultados", index=False)
                pd.DataFrame([{"Métrica": k, "Valor": v}
                              for k, v in summary.items()]).to_excel(
                    writer, sheet_name="Resumo", index=False)

            now = datetime.now(timezone.utc)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,started_at,finished_at,status,mode,"
                    "pipeline_version,rules_hash,model,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    ("run_locked", (now - timedelta(minutes=4)).isoformat(),
                     (now - timedelta(minutes=2)).isoformat(), "FAILED",
                     "classify", p.PIPELINE_VERSION, rules.hash,
                     "jev:jev-latest",
                     "BASELINE_PERSISTENCE_FAILED: PermissionError: BASELINE_FILE_LOCKED"),
                )
                for idx, row in enumerate(records):
                    conn.execute(
                        "INSERT INTO classification_results("
                        "checkpoint_id,business_key,brand,model,raw_hash,"
                        "rules_hash,pipeline_version,result_json,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(idx), p.business_key(row["Marca"],row["Modelo"]),
                         row["Marca"], row["Modelo"], "raw", rules.hash,
                         p.PIPELINE_VERSION, json.dumps(row), now.isoformat()),
                    )
                conn.commit()
            extracted, keys = read_result(result, 4, rules, db_path=db)
            self.assertEqual(len(extracted), 4)
            self.assertEqual(len(keys), 4)
            # Regression: a second successful 338-like run rewrites the Excel.
            # Do NOT reuse the older Excel-lock failure as provenance.
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,started_at,finished_at,status,mode,"
                    "pipeline_version,rules_hash,model,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    ("run_newer", (now - timedelta(minutes=1)).isoformat(),
                     (now - timedelta(seconds=20)).isoformat(), "COMPLETED",
                     "classify", p.PIPELINE_VERSION, rules.hash,
                     "jev:jev-latest",
                     json.dumps({"Pilot Cohort Hash": cohort_hash,
                                 "Elegíveis": 4})),
                )
                conn.commit()
            extracted, keys = read_result(result, 4, rules, db_path=db)
            self.assertEqual(len(extracted), 4)
            self.assertEqual(len(keys), 4)
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE classification_results SET result_json=? "
                             "WHERE checkpoint_id='0'",
                             (json.dumps({**records[0], "FAMILY": "ENGINE"}),))
                conn.commit()
            with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_CONFLICT"):
                read_result(result, 4, rules, db_path=db)

    def test_wrong_failure_reason_blocks_replay_before_any_writes(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d)
            rules=p.Rules(Path(p.__file__).with_name("regras_negocio.json"))
            with pd.ExcelWriter(result) as w:
                pd.DataFrame([{"Marca":"BRAND","Modelo":"SKU-00000",
                               "Resolution State":"RESOLVED"}]).to_excel(
                               w,sheet_name="Resultados",index=False)
                pd.DataFrame([{"Métrica":"cohort_size","Valor":1},
                              {"Métrica":"Baseline Warning","Valor":"OTHER"},
                              {"Métrica":"Rules Hash","Valor":rules.hash},
                              {"Métrica":"Pipeline Version","Valor":p.PIPELINE_VERSION}]).to_excel(
                              w,sheet_name="Resumo",index=False)
            with self.assertRaisesRegex(RuntimeError,"RESULT_NOT_FAILED_BY_EXCEL_LOCK"):
                read_result(result,1,rules)


class New50PilotTests(unittest.TestCase):
    def test_prepare_is_50_fresh_diverse_and_does_not_touch_canonical(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d)
            source=Path(d)/"All_19k.xlsx"
            data=[{"Marca":f"BRAND{i%70}","Modelo":f"SKU-{i:06d}"} for i in range(15005)]
            data += [data[0],data[1]]
            pd.DataFrame(data).to_excel(source,index=False)
            initial={x.name:sha(x) for x in (master,final,cache,db)}
            args=argparse.Namespace(config=str(cfg),source=str(source),count=50,
                                    max_calls=65,max_tokens=350000)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(prepare(args),0)
            manifests=list((Path(d)/"pilotos").glob("*/manifest.json"))
            self.assertEqual(len(manifests),1)
            manifest=json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["count"],50)
            self.assertEqual(len(set(x["key"] for x in manifest["selected"])),50)
            self.assertGreaterEqual(manifest["readiness"]["represented_brands"],30)
            self.assertEqual(manifest["official_writes"],False)
            self.assertEqual(initial,{x.name:sha(x) for x in (master,final,cache,db)})
            with patch("scripts.pilot_50_full_gate.subprocess.Popen",
                       side_effect=AssertionError("No paid calls")) as popen:
                with self.assertRaisesRegex(RuntimeError,"PILOT_EXPLICIT_CONFIRMATION_REQUIRED"):
                    execute(argparse.Namespace(manifest=str(manifests[0]),confirm=""))
                popen.assert_not_called()

    def test_configured_input_1030_selects_fifty_without_alternate_source(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d,input_count=1030)
            args=argparse.Namespace(config=str(cfg),source="",count=50,
                                    max_calls=65,max_tokens=350000)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(prepare(args),0)
            self.assertIn("PILOT_50_PREPARED_NO_APIS",output.getvalue())
            manifests=list((Path(d)/"pilotos").glob("*/manifest.json"))
            self.assertEqual(len(manifests),1)
            m=json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(m["readiness"]["source_rows"],1030)
            self.assertEqual(m["source"],str(inp))
            self.assertEqual(len(m["selected"]),50)

    def test_only_forty_nine_valid_input_skus_fail_without_api(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d,input_count=49)
            args=argparse.Namespace(config=str(cfg),source="",count=50,
                                    max_calls=65,max_tokens=350000)
            with self.assertRaisesRegex(RuntimeError,"PILOT_INPUT_INSUFFICIENT"):
                prepare(args)
            self.assertFalse((Path(d)/"pilotos").exists())

    def test_fifty_unseen_skus_required_no_repeat_spending(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d,input_count=1030)
            writer=p.TavilyWritableStore(cache)
            try:
                for i in range(1030):
                    writer.put("BRAND",f"SKU-{i:05d}",{
                        "results":[{"title": "Synthetic old data"}],
                        "_collection":{"state":"NO_TARGET_EVIDENCE"},
                    })
            finally:
                writer.close()
            args=argparse.Namespace(config=str(cfg),source="",count=50,
                                    max_calls=65,max_tokens=350000)
            with self.assertRaisesRegex(RuntimeError,
                                         "PILOT_FRESH_COHORT_INSUFFICIENT"):
                prepare(args)
            self.assertFalse((Path(d)/"pilotos").exists())



class MassReleaseManualQATests(unittest.TestCase):
    def test_cannot_release_without_ten_human_approvals(self):
        with TemporaryDirectory() as d:
            base=Path(d)
            source=base/"19k.xlsx"
            source.write_bytes(b"source frozen")
            pilot=base/"pilot"
            pilot.mkdir()
            quality=pilot/"pilot_50_quality_review.xlsx"
            review=[{"Marca":"BRAND","Modelo":f"M-{i:03d}",
                     "Auditoria Manual (OK/ERRO)":"",
                     "Título Real na Base Final":f"Brand product {i}",
                     "O que é (Tipo)":"DRILL",
                     "Category General":"OTHERS","FAMILY":"DRILLERS"}
                    for i in range(10)]
            with pd.ExcelWriter(quality) as w:
                pd.DataFrame(review).to_excel(w,sheet_name="Amostra_10_Validar",index=False)
                pd.DataFrame(review*5).to_excel(w,sheet_name="Todos_50",index=False)
            report=pilot/"pilot_50_result.json"
            report.write_text(json.dumps({
                "status":"TECHNICAL_PASS_AWAITING_QUALITY_REVIEW",
                "audited":{"complete_classifications":40,
                           "search_titles_missing_in_final":0,
                           "prior_accepted_preserved":100,
                           "bi_locks_preserved":20,
                           "quality_review_file":str(quality)},
            }),encoding="utf-8")
            (pilot/"manifest.json").write_text(json.dumps({
                "source":str(source),"source_sha256":sha(source),
                "state":"TECHNICAL_PASS_AWAITING_QUALITY_REVIEW",
            }),encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,"PILOT_QA_NOT_MANUALLY_APPROVED"):
                verify_mass_gate(report,source)
            for rec in review:
                rec["Auditoria Manual (OK/ERRO)"]="OK"
            with pd.ExcelWriter(quality) as w:
                pd.DataFrame(review).to_excel(w,sheet_name="Amostra_10_Validar",index=False)
                pd.DataFrame(review*5).to_excel(w,sheet_name="Todos_50",index=False)
            self.assertEqual(verify_mass_gate(report,source)["human_reviews_approved"],10)
            source.write_bytes(b"modified")
            with self.assertRaisesRegex(RuntimeError,"MASS_SOURCE_CHANGED_AFTER_PILOT_50"):
                verify_mass_gate(report,source)



if __name__=="__main__":
    unittest.main()
