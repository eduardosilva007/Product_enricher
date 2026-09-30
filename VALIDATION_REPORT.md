# R9.6p8.1 Product Definition Pilot — Validation Report

Validation executed locally on 2026-09-29.

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
```

## Prompt contract

The LLM is instructed to **define the primary product represented by the supplied evidence** using the most specific technical/commercial product name supported by the evidence. It is explicitly forbidden from forcing the answer into the internal taxonomy or from returning only an industry/use/function/generic class when a specific product name is evidenced.

The pilot remains read-only for Base Final and Consolidado and makes zero Search-provider requests.
