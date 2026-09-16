# R9.5 HF3 — Recovery Ladder / Title Rescue / Taxonomy Consensus

- Diferencia presença de cache de evidência realmente utilizável (`Collection State`, `Raw Search Results`, `Curated Sources`).
- Adiciona `TITLE_ONLY_RESCUE`: títulos de Search STRONG/PROBABLE podem gerar candidatos quando o Extract/Target Isolation falha, sem reutilizar BODY RAW.
- Bloqueia title rescue para `REJECT_WRONG_MODEL`, `REJECT_COMPATIBILITY_ONLY` e `REJECT_RELATED_ONLY`.
- Adiciona `Título Referência` para apoio humano mesmo quando nenhuma fonte pode ser promovida a `Título Real`.
- Adiciona `Próxima Ação` para distinguir `JSON_GAP_OU_LLM_GAP`, recoleta e revisão de TYPE.
- Adiciona consenso taxonômico: TYPE pode continuar em revisão enquanto Category/FAMILY são preenchidos quando todos os candidatos convergem para o mesmo par.
- Corrige `Taladro Atornillador` como drill-driver (`DRILL`) e impede `SCREWDRIVER` de roubar a frase composta.
- Adiciona regras estreitas de séries já sustentadas pela amostra validada: LOWARA `1016LC/1016LD` e WILO `MEDANA CH1` -> `CENTRIFUGAL PUMP`; SHIMGE `PW*Z` -> `PERIPHERAL PUMP`.
- Mantém LLM sem poder inventar Category/FAMILY e preserva `Revisão B.I=OK` como HARD LOCK absoluto.

# R9.5 HF2 — 2026-09-15

- Added `TAXONOMY_EVIDENCE_RULES` with executable Python support.
- Removed automatic `WATERPUMPS | WATERPUMP` taxonomy for `WATERPUMP`.
- Removed generic `WATER BOOSTER PUMP` fallback for `LIFT PUMP`.
- Preserved business TYPE as `WATERPUMP` / `LIFT PUMP` while deriving specific FAMILY from target-approved evidence.
- Added pump subtype rules for centrifugal, peripheral, submersible, circulation, pool, priming, jet, pressure booster, pressurization, inverter booster and booster families.
- Added high-confidence brand/series refinements learned from manually validated cases.
- Ambiguous/generic pump evidence now remains REVIEW instead of being auto-taxonomized.
- Added regression gates; validation: 69/69 synthetic, 16/16 identity, 22/22 explicit real cases.
- On the 136 manually corrected pump cases, title-only simulation auto-resolved 39 and all 39 matched the validated FAMILY (0 false auto-resolutions).

# R9.5 Collection Quality — Changelog

## 3.9.5-hf1 — Operational suffix identity + explicit title coverage

### SUMMARY

fix: recover explicit title classifications without weakening SKU identity

### DESCRIPTION

- Removes trailing operational voltage/frequency tokens from Search Model only when the remaining core is model-like; original Marca/Modelo and business_key remain unchanged.
- Adds DONG CHENG 220V operational-suffix handling for models such as DJG02 355 220V -> DJG02 355.
- Keeps voltage that is structurally inside the actual model, such as GSA 18V 32.
- Makes alias matching tolerant to structural separators (space, hyphen, slash, dot, underscore) without fuzzy matching.
- Adds CUT OFF MACHINE variants to CUT-OFF SAW.
- Adds SCREWDRIVER as a deterministic product type mapped to OTHERS | POWER TOOLS, with impact-driver safeguards.
- Adds specificity OSCILLATING MULTI-TOOL > MULTI-TOOL and CORDLESS IMPACT > SCREWDRIVER.
- Adds 8 DONG CHENG synthetic regressions and 2 identity regressions.
- Requires R9.5 `_collection` evidence before runtime classification; legacy RAW cache is now PENDING_CURATION instead of bypassing the Collection Quality Gate.
- Preserves strict behavior when a descriptive title is not actually anchored to the target SKU.
- Preserves BLOWER power safety: 680W alone does not imply CORDED; explicit 220V/mains evidence does.

## SUMMARY

feat: add R9.5 collection quality gate and target isolation funnel

## DESCRIPTION

- Keeps product search fully open with no domain, country or strict language filtering and exact_match disabled.
- Splits discovery from evidence consumption using Tavily Search followed by selective Tavily Extract.
- Adds deterministic SKU identity validation before TYPE/POWER detection.
- Adds Target Isolation Funnel for catalogues, listings and multi-product pages instead of discarding them by default.
- Prevents other SKUs, related products and compatibility-only references from contaminating target evidence.
- Stores Search/Extract RAW for audit while Stage 1 consumes only curated_results.
- Adds selective Advanced Extract retry for catalogues/tables that Basic Extract cannot isolate.
- Adds PRIMARY, FALLBACK and ADVANCED RESCUE search stages without injecting inferred product type into the query.
- Adds source/query audit, Collection State and Tavily credit telemetry.
- Adds cache-only R9.5 curation mode with zero Tavily credits.
- Preserves Stage 1/Stage 1.5/LLM architecture and BI hard lock; HF1 only extends controlled rule coverage for proven title gaps.

