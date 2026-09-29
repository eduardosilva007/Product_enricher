# VALIDATION REPORT — R9.6p6

Date: 2026-09-29

## Static / schema
- `python -m py_compile pipeline_produtos.py`: PASS
- `python pipeline_produtos.py --mode validate`: PASS
- Rules version: `2026-09-29-v3.0.9-r9.6p6-classification-recovery`
- Collection policy: `2026-09-29-r9.6p6-clean-identity-v1`

## Quality gates
- Synthetic: 70/70 PASS
- Identity: 16/16 PASS
- Real explicit: 22/22 PASS
- Real reference-only: 12 retained as reference-only

## Regression suites
- `test_collection_single_request_r96.py`: PASS
- `test_collection_source_sieve_r96p5.py`: PASS
- `test_recovery_hf3.py`: PASS
- `test_taxonomy_hf2.py`: PASS
- `test_baseline_export_hf3_1p2.py`: PASS
- `test_classification_recovery_r96p6.py`: PASS

## Critical invariants preserved
- Maximum 1 Search-provider request per product.
- No automatic Search retry, secondary query, provider failover, Tavily Extract or Advanced.
- `Revisão B.I = OK` hard lock unchanged.
- LLM still cannot invent TYPE or taxonomy.
- Wrong-SKU near matches remain blocked (no fuzzy edit-distance identity).

## New R9.6p6 protections
- Query punctuation normalization does not destroy identity variants such as decimal revisions or slash alternate identifiers.
- `TAXONOMY_EVIDENCE_RULES` may recover TYPE candidates only when target-anchored.
- Explicit multi-tool combo kits can resolve to `POWER TOOL COMBO KIT`.
- Single-tool kits with battery/charger/accessories do not resolve as combo kits.
- Base Final contains one title field (`Título Real`); reference-title metadata stays audit-only.
