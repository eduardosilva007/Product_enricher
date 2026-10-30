#!/usr/bin/env python3
"""Read-only release guard for JEV/Clef routing; never calls a paid API."""
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
source=(ROOT/"pipeline_produtos.py").read_text(encoding="utf-8")
tree=ast.parse(source,filename="pipeline_produtos.py")
cfg=__import__("json").loads((ROOT/"pipeline_config.json").read_text(encoding="utf-8"))
errors=[]
def require(cond,msg):
    if not cond:errors.append(msg)
funcs={n.name:n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
require("llm_provider" in source,"ausente roteamento do provedor")
require("--llm-provider" in source,"ausente argumento CLI")
require("_menu_llm_provider" in funcs,"menu de classificação ausente")
require('provider=llm_provider' in source,"instanciação não usa provedor selecionado")
require('self.provider == "jev"' in source,"implementação HTTP Jev ausente")
require('"/v1/systemone"' in source,"endpoint JEV ausente")
require('result = body if self.provider == "jev"' in source,"parser da resposta Jev ausente")
require(cfg.get("classification_v42",{}).get("default_provider") == "jev","JEV não é padrão no config")
require(cfg.get("jev",{}).get("api_key_env") == "TYPESAFE_API_KEY","env de API JEV ausente")
require('self.db.get_llm(' in source and 'self.db.put_llm(' in source,"cache persistente ausente")
require('self.provider, self.cache_model_id' in source,"separação de cache por provedor ausente")
# Import + arg parser evaluation: no HTTP and no model loads.
import sys
sys.path.insert(0,str(ROOT))
from pipeline_produtos import build_parser
parser=build_parser()
for provider in ("jev","clef","local"):
    args=parser.parse_args(["--mode","classify","--llm-provider",provider,"--max-items","1"])
    require(args.llm_provider==provider,f"parser não aceita {provider}")
if errors:
    for msg in errors:print("FALHOU:",msg)
    raise SystemExit(1)
print("ROTEAMENTO JEV/CLEF/LOCAL: checagens estáticas OK; nenhuma chamada remota realizada.")
print("ATENÇÃO: sucesso neste teste NÃO comprova autenticação nem resposta real da API TypeSafe.")
