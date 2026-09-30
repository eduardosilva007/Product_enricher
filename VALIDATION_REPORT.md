# R9.6p8.3 Product Definition Pilot — Validation Report

Validation executed locally on 2026-09-30.

## Results

```text
py_compile ................................ PASS
pipeline --mode validate .................. PASS
Synthetic quality gates ................... 80/80
Identity quality gates .................... 17/17
Real explicit coverage .................... 22/22 (34 active; 12 reference-only)
Semantic/Product Definition GOLD ......... 6/6
Single-request Search policy .............. PASS
Source Sieve + enrichment ................. PASS
HF3 recovery .............................. PASS
HF2 taxonomy .............................. PASS
Baseline export ........................... PASS
R9.6p6 classification recovery ............ PASS
R9.6p8 import-noise taxonomy .............. PASS
R9.6p8.1 Product Definition contract ...... PASS
R9.6p8.2 pilot telemetry code ............. PASS
R9.6p8.3 eligible cohort/JSON contract ..... PASS
```

## R9.6p8.2 operational changes

- Per-item START/END telemetry is emitted for every pilot branch.
- LLM DEFINE and LLM MAP calls emit START/END with provider/model/cache/duration.
- Provider pacing waits are visible in the log.
- Partial pilot workbooks are checkpointed every 5 rows and on interruption/failure.
- Interactive default pilot size is 10 items.
- No Search-provider call is introduced by the semantic pilot.
- Base Final and Consolidado remain read-only in semantic mode.

The Product Definition prompt and taxonomy rules are unchanged from R9.6p8.1; this is an operational/telemetry patch.

## R9.6p8.3 validation additions

- Confirms semantic JSON missing `decision` is rejected by the contract validator.
- Confirms valid semantic JSON passes the contract validator.
- Confirms Product Definition cache namespace is bumped to v3.
- Confirms semantic pilot target states are UNRESOLVED/AMBIGUOUS.
- Confirms Product Definition prompt receives only PROBABLE/STRONG sources.
- Full prior regression suite remains green.

