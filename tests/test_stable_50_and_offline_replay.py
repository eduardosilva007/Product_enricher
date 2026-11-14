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

    def test_1030_input_does_not_masquerade_as_19k(self):
        with TemporaryDirectory() as d:
            cfg, master, final, cache, db, inp, result=local_fixture(d,input_count=1030)
            args=argparse.Namespace(config=str(cfg),source=str(inp),count=50,
                                    max_calls=65,max_tokens=350000)
            with self.assertRaisesRegex(RuntimeError,"PILOT_SOURCE_TOO_SMALL"):
                prepare(args)


if __name__=="__main__":
    unittest.main()
