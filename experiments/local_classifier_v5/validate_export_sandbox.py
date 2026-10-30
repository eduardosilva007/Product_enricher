#!/usr/bin/env python3
"""Isolated end-to-end export regression. Runs sync-input only on copies."""
import collections,json,subprocess,sys,tempfile
from pathlib import Path
from shutil import copy2
from openpyxl import load_workbook

ROOT=Path(__file__).resolve().parents[2]
FILES=["Base de Produtos.xlsx","Base Final Produtos Enriquecida.xlsx","Consolidado Base de Produtos Enriquecida.xlsx"]
def rows(path):
    wb=load_workbook(path,read_only=True,data_only=True)
    sh=wb.active
    data=sh.iter_rows(values_only=True)
    heads=[str(v or '').strip() for v in next(data)]
    index={n:i for i,n in enumerate(heads)}
    def val(row,key):
        i=index.get(key)
        return str(row[i]).strip() if i is not None and i<len(row) and row[i] is not None else ''
    return [(val(r,"Marca") or val(r,"Fabricante"),val(r,"Modelo"),
             val(r,"Revisão B.I"),val(r,"O que é (Tipo)"),val(r,"Revisão"),val(r,"Título Real"),
             val(r,"Category General"),val(r,"FAMILY")) for r in data]
def main():
    for name in FILES:
        if not (ROOT/name).is_file(): raise SystemExit("Arquivo obrigatório ausente: "+name)
    original=rows(ROOT/FILES[1])
    bi_before={(r[0],r[1]):r for r in original if r[2].upper()=="OK"}
    with tempfile.TemporaryDirectory(prefix="product_enricher_safe_") as work:
        temp=Path(work)
        for name in FILES:copy2(ROOT/name,temp/name)
        cfg=json.loads((ROOT/"pipeline_config.json").read_text(encoding="utf-8"))
        for key,name in [("input",FILES[0]),("final",FILES[1]),("master",FILES[2])]:
            cfg["files"][key]=str(temp/name)
        cfg["files"]["baseline_backup_dir"]=str(temp/"backups")
        cfg["files"]["baseline_audit"]=str(temp/"audit.csv")
        config=temp/"safe_config.json"
        config.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
        command=[sys.executable,str(ROOT/"pipeline_produtos.py"),"--mode","sync-input","--config",str(config),"--yes"]
        print("SIMULAÇÃO EM CÓPIAS:",temp,flush=True)
        result=subprocess.run(command,cwd=ROOT,check=False)
        if result.returncode:raise SystemExit("FALHA: sincronização isolada retornou "+str(result.returncode))
        source=rows(temp/FILES[0]);after=rows(temp/FILES[1])
        count_source=collections.Counter((r[0],r[1]) for r in source)
        count_final=collections.Counter((r[0],r[1]) for r in after)
        missing={key:n-count_final[key] for key,n in count_source.items() if count_final[key]<n}
        false_approvals=[r[:2] for r in after if r[4].upper()=="AUTO_APROVADO" and
                         (not r[3] or r[3].upper() in {"REVISAR","UNKNOWN","N/A"} or not r[5] or not r[6] or not r[7])]
        bi_after={(r[0],r[1]):r for r in after if r[2].upper()=="OK"}
        changed_bi=[key for key,val in bi_before.items() if key not in bi_after or bi_after[key]!=val]
        print(json.dumps({"input_rows":len(source),"final_rows":len(after),
            "missing_input_occurrences":sum(missing.values()),"missing_examples":list(map(str,list(missing.items())[:10])),
            "false_auto_approvals":len(false_approvals),"false_approval_examples":false_approvals[:10],
            "changed_BI_OK_rows":len(changed_bi),"changed_BI_examples":changed_bi[:10]},ensure_ascii=False,indent=2))
        if missing or false_approvals or changed_bi:raise SystemExit("BLOQUEADO: falha na regressão; base de produção NÃO alterada")
    print("PASSOU: cobertura de linhas, aprovações e hard-lock B.I; produção não alterada")
if __name__=="__main__":main()
