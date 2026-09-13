# R9.5 Collection Quality — Changelog

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
- Preserves the R9.4 business-rules JSON, Stage 1, Stage 1.5, taxonomic mapping and BI hard lock.
