# R9.5 HF3.1 — Runtime Hotfix Validation

HF3.1 fixes a runtime integration defect introduced by HF3: `finalize_product()` requires `cfg` before the LLM argument, but `run_pipeline()` still called it with the previous signature.

Validated after patch:

- `python -m py_compile pipeline_produtos.py` -> OK
- `test_recovery_hf3.py` -> OK
- `test_collection_quality.py` -> OK
- `test_taxonomy_hf2.py` -> OK
- Synthetic quality gates -> 70/70
- Identity quality gates -> 16/16
- Explicit real regression cases -> 22/22

Pipeline version: `3.9.5-hf3.1`
Rules version unchanged: `2026-09-15-v3.0.8-r9.5-hf3-recovery-ladder`

---

# R9.5 HF3 — Validation Report

## Source sample reviewed

Input reviewed: `Markdown(8).md colado` (1,145 pending rows from the HF2 cache recovery run).

Observed result profile:

- 1,145 pending rows analyzed.
- 606 rows had no `Título Real`.
- 539 rows had a title.
- 463 rows had a title but blank `Category General` / `FAMILY`.
- Within those 463 rows:
  - 422 had `O que é (Tipo)=REVISAR`.
  - 40 had `O que é (Tipo)=WATERPUMP` but taxonomy remained blank.
  - 1 had `O que é (Tipo)=LIFT PUMP` but taxonomy remained blank.
- Only 76 of the 1,145 rows were fully classified with nonblank Category/FAMILY in that run.

## Root causes confirmed

1. `RAW_OK` was being treated operationally as “cache usable”, even when `_collection.curated_results` was empty.
2. When Collection Quality rejected every source, the final result lost the useful Search title even if a target-looking title existed in RAW.
3. When multiple TYPE candidates existed, the pipeline blanked Category/FAMILY even when every candidate mapped to the same taxonomy pair.
4. `no_candidate_means_no_llm` correctly protects accuracy, but it creates a dead-end for strong target titles whose terminology is missing from the JSON.
5. HF2 pump safety removed the dangerous generic `WATERPUMPS | WATERPUMP`, but several validated product-series families still needed narrow evidence rules.

## HF3 changes validated

### Title-only rescue

If Extract / Target Isolation cannot produce curated body evidence, a Search title can now be used for deterministic candidate generation only when identity is `STRONG` or `PROBABLE`.

Safety constraints:

- RAW body/snippet is not used by the rescue layer.
- `REJECT_WRONG_MODEL`, `REJECT_COMPATIBILITY_ONLY` and `REJECT_RELATED_ONLY` remain blocked.
- Weak numeric/model collisions are not promoted.

Validated examples:

- `DONG CHENG | DJG02 355 220V | Dongcheng DJG02-355 Electric Cut Off Machine...` -> `CUT-OFF SAW`.
- `RONIX | 8600C | 8600C - White Inspectors Lisle Cotton Gloves` -> remains unresolved because identity is WEAK; title is available only as reference.

### Taxonomy consensus

When TYPE is still ambiguous, HF3 can populate Category/FAMILY if every surviving TYPE candidate maps to exactly the same valid taxonomy pair. `Review Required` remains `SIM`.

Example:

- `DRILL + SCREWDRIVER` -> `OTHERS | POWER TOOLS` while exact TYPE can remain under review.

### Drill-driver phrase fix

`Taladro Atornillador` / `Taladro/Atornillador` now resolves as `DRILL`, while `SCREWDRIVER` explicitly rejects those compound phrases.

Validated:

- `PEKTRA | PKCD1210CK | Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v` -> `DRILL | BATTERY | OTHERS | POWER TOOLS`.

### Pump series safety

Added narrow brand+model rules supported by the manually reviewed sample:

- LOWARA `1016LC* / 1016LD*` -> `OTHERS | CENTRIFUGAL PUMP`.
- WILO `MEDANA CH1*` -> `OTHERS | CENTRIFUGAL PUMP`.
- SHIMGE `PW*Z` lift-pump series -> `OTHERS | PERIPHERAL PUMP`.

Of the 41 rows that already had `WATERPUMP/LIFT PUMP` but blank taxonomy in the HF2 sample, HF3 maps 37 deterministically and leaves 4 unresolved rather than guessing.

## Strong-title taxonomy-gap finding

Among the 463 title-present rows with blank taxonomy:

- 333 titles have STRONG/PROBABLE identity using Brand+Model anchoring.
- 302 of those still produce zero TYPE candidates with the current JSON vocabulary.

This means the next residual bottleneck is not primarily Tavily. It is the combination of:

- missing multilingual/product aliases in `regras_negocio.json`;
- the intentional rule that blocks LLM when no deterministic TYPE candidate exists.

HF3 therefore exposes `Próxima Ação`, including `JSON_GAP_OU_LLM_GAP`, so these rows are no longer indistinguishable from true recollection failures.

## Regression validation

- `python -m py_compile pipeline_produtos.py` -> OK
- `Rules.validate()` -> 0 errors
- Synthetic quality gates -> 70/70
- Identity quality gates -> 16/16
- Explicit real regression cases -> 22/22
- Collection Quality test suite -> OK
- HF3 Recovery test suite -> OK

Pipeline version: `3.9.5-hf3`
Rules version: `2026-09-15-v3.0.8-r9.5-hf3-recovery-ladder`
