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
