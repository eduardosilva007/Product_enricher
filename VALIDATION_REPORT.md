# VALIDATION REPORT — R9.7 Cache-First Free-Tier

Date: 2026-09-30

## Static / core

- `python -m py_compile pipeline_produtos.py`: PASS
- `python pipeline_produtos.py --mode validate`: PASS

## Quality gates

- Synthetic: 84/84 PASS
- Identity: 17/17 PASS
- Real explicit: 22/22 PASS
- Real reference-only: 12 (informational)
- Semantic GOLD: 6/6 PASS

## Regression suites

- R9.6 single-request cost policy: PASS
- R9.6p4 source sieve + local enrichment: PASS
- R9.5 HF3 recovery: PASS
- R9.5 HF2 taxonomy: PASS
- HF3.1p2 baseline export: PASS
- R9.6p7 import-noise taxonomy: PASS
- R9.7 cache-first free-tier: PASS

## R9.7 targeted validations

PASS:
- `www.XYtronic-USA.com` is detected as generic title.
- Explicit `TCL` source against target `HYUNDAI` is detected as cross-brand conflict.
- TF-IDF fallback retrieves `CROSS FLOW FAN` for explicit cross-flow evidence.
- TF-IDF fallback retrieves `SOLDERING STATION` for explicit soldering-station evidence.
- TF-IDF fallback retrieves `WINDSCREEN WASHER RESERVOIR` for explicit reservoir evidence.
- `WINDSCREEN WASHER PUMP` is suppressed when evidence explicitly says washer-fluid reservoir.
- New types map to valid deterministic taxonomy pairs.

## Synthetic cache integration smoke

A local SQLite cache was created only for validation and was not included in the package.
The cache-first mode completed with:

- Search API calls: 0
- LLM/API calls: 0
- Sentence Transformers unavailable in validation environment -> clean fallback to scikit-learn TF-IDF
- Explicit strong CROSS FLOW FAN -> `LOCAL_EXACT_ALIAS_ACCEPTED`
- HYUNDAI target with TCL source -> `CACHE_IDENTITY_GAP`
- No writes to Base Final / Consolidado

## Not live-validated here

- The user's real `cache_tavily.sqlite` is not present in this environment.
- The optional multilingual Sentence Transformers model is not installed in this environment.
- Therefore final retrieval quality over the user's full real cache must be measured by running option 8 on the user's machine.
