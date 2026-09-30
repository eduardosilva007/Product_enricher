# VALIDATION REPORT — R9.6p7

Date: 2026-09-29

## Static / schema
- `python -m py_compile pipeline_produtos.py`: PASS
- `python pipeline_produtos.py --mode validate`: PASS
- Rules version: `2026-09-29-v3.0.10-r9.6p7-import-noise-taxonomy`
- Collection policy: `2026-09-29-r9.6p7-brand-primary-gate-v1`

## Quality gates
- Synthetic: 80/80 PASS
- Identity: 17/17 PASS
- Real explicit: 22/22 PASS
- Real reference-only: 12 retained as reference-only

## Regression suites
- `test_collection_single_request_r96.py`: PASS
- `test_collection_source_sieve_r96p5.py`: PASS
- `test_recovery_hf3.py`: PASS
- `test_taxonomy_hf2.py`: PASS
- `test_baseline_export_hf3_1p2.py`: PASS
- `test_classification_recovery_r96p6.py`: PASS
- `test_import_noise_taxonomy_r96p7.py`: PASS

## R9.6p7 targeted protections
- Brand/model mismatch cannot auto-approve even when an exact model appears in the source.
- Company legal suffix normalization remains deterministic and non-fuzzy.
- Industrial/venturi blowers cannot inflate garden BLOWER categories.
- Fan/cooler import noise routes to OTHERS with a specific physical family.
- Windscreen/ink/fuel/oil/vacuum/condensate pumps are blocked from WATERPUMP.
- TILLER can close to TILLERS|TILLERS with UNKNOWN power.
- Resolved rows force `Próxima Ação=CONCLUIDO`.
- Resolved `Título Real` is selected only from a STRONG source supporting the TYPE.

## Critical invariants preserved
- Maximum 1 Search-provider request per product.
- No automatic Search retry, secondary query, provider failover, Tavily Extract or Advanced.
- `Revisão B.I = OK` hard lock unchanged.
- LLM cannot invent TYPE or taxonomy.
- Wrong-SKU near matches remain blocked; no fuzzy edit-distance identity.
