#!/usr/bin/env python3
"""Read-only taxonomy preflight. Does not modify rules, Gold or production files."""
import argparse,collections,csv,json,sys
from pathlib import Path
def norm(s):
 import unicodedata,re
 s=''.join(c for c in unicodedata.normalize('NFKD',str(s)) if not unicodedata.combining(c))
 return re.sub(r'\s+',' ',s.strip().casefold())
def main():
 p=argparse.ArgumentParser()
 p.add_argument('--rules',default='regras_negocio.json')
 p.add_argument('--config',default='pipeline_config.json')
 p.add_argument('--out',default='Taxonomy_Audit_V1')
 a=p.parse_args();root=Path(__file__).resolve().parents[2]
 def path(v):
  x=Path(v);return x if x.is_absolute() else root/x
 rules=json.loads(path(a.rules).read_text(encoding='utf-8'))
 config=json.loads(path(a.config).read_text(encoding='utf-8'))
 out=path(a.out);out.mkdir(parents=True,exist_ok=True)
 findings=[]
 def add(severity,code,where,detail):
  findings.append(dict(severity=severity,code=code,location=str(where),detail=str(detail)))
 version=rules.get('VERSION');expected=config.get('release_contract',{}).get('rules_version')
 if version!=expected:add('BLOCKER','RELEASE_VERSION_MISMATCH','release_contract.rules_version',f'expected={expected}; actual={version}')
 pairs=rules.get('TAXONOMY_PAIRS',[])
 pairkeys=[(x.get('category_general'),x.get('family')) for x in pairs]
 pairset=set(pairkeys)
 for pair,n in collections.Counter(pairkeys).items():
  if n>1:add('HIGH','DUPLICATE_PAIR',pair,f'count={n}')
 types=rules.get('PRODUCT_TYPES',{})
 for i,r in enumerate(rules.get('TAXONOMY_RULES',[])):
  key=(r.get('category_general'),r.get('family'))
  if key not in pairset:add('BLOCKER','RULE_UNKNOWN_PAIR',i,str(key))
  if r.get('primary_type') not in types:add('BLOCKER','RULE_UNKNOWN_TYPE',i,str(r.get('primary_type')))
  if r.get('power')=='*' and key[1] in {'POWER TOOLS','CUTTER','MULTIFUNCTIONAL','ACCESSORIES','OTHERS','PARTS'}:
   add('REVIEW','BROAD_WILDCARD_RULE',i,f'type={r.get("primary_type")}; pair={key}; priority={r.get("priority")}')
 aliases=collections.defaultdict(set)
 for t,obj in types.items():
  for alias in obj.get('aliases',[]) or []:
   if norm(alias):aliases[norm(alias)].add(t)
 for alias,ts in sorted(aliases.items()):
  if len(ts)>1:add('HIGH','CROSS_TYPE_ALIAS',alias,' / '.join(sorted(ts)))
 ambiguous={'OTHERS','ACCESSORIES','PARTS','POWER TOOLS','MULTIFUNCTIONAL','CUTTER','SAW','ENGINE','GENERATOR','WATER PUMP'}
 for category,family in pairkeys:
  if family in ambiguous:add('REVIEW','GENERIC_OR_OVERLAPPING_FAMILY',f'{category}|{family}','Requires evidence-based specificity check; do not delete automatically')
 with (out/'findings.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=['severity','code','location','detail'],delimiter=';');w.writeheader();w.writerows(findings)
 counts=dict(collections.Counter(x['severity'] for x in findings))
 result={'rules_version':version,'config_rules_version':expected,'taxonomy_pairs':len(pairs),'taxonomy_rules':len(rules.get('TAXONOMY_RULES',[])),'product_types':len(types),'findings_by_severity':counts,'findings_file':str(out/'findings.csv'),'audit_only':True}
 (out/'summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False,indent=2))
 return 2 if counts.get('BLOCKER') else 0
if __name__=='__main__':sys.exit(main())
