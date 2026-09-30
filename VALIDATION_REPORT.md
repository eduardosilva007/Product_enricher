# R9.6p8.4 Product Definition Pilot — Validation Report

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
R9.6p8.4 eligible cohort/contract .......... PASS
R9.6p8.4 Groq auth preflight ............... PASS
R9.6p8.4 NVIDIA explicit-schema prompt ..... PASS
```

## Failure reproduced from user log

The observed path was:

```text
NVIDIA_OUTPUT_PARSE
    -> automatic fallback
GROQ 401 AUTH Invalid API Key
    -> uncaught OperationalPause / traceback
```

R9.6p8.4 prevents the second failure by validating Groq authentication before the pilot starts. It also gives NVIDIA the exact output schema it is later validated against. If the active provider route still cannot complete safely, the pilot writes a partial workbook and terminates cleanly without modifying Base Final or Consolidado.

No live provider call was executed in the validation environment; provider behavior is covered with mocked regression tests plus the existing offline suite.
