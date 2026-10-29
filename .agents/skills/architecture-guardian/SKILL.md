# Architecture Guardian

## Goal
Protect Product Enricher architecture from responsibility drift and SKU-specific patches.

## Mandatory flow
IDENTITY -> ROLE -> RULE ENGINE -> ENTITY EXTRACTION -> SEMANTIC RETRIEVAL -> DECISION PROVIDER -> TAXONOMY

## Contracts
- Identity is independent from TYPE evidence.
- GLiNER is EntityExtractor only: raw spans such as product_type, power_source, voltage.
- GLiNER never chooses canonical TYPE, role, combo, Category General, FAMILY, or AUTO eligibility.
- Rule Engine may close only target-supported deterministic evidence.
- BGE-M3 retrieves/ranks canonical TYPE candidates.
- Reranker only reorders candidates.
- Local semantic output cannot auto-close until calibrated on frozen Gold.
- JEV/Clef/other LLM is a DecisionProvider and must choose only inside a closed schema.
- Provider/runtime failures are OPERATIONAL_ERROR, not product uncertainty.
- Gold is evaluation-only and must never influence production classification.
- No Brand+Model/SKU-specific production exception may be added to make an acceptance row pass.

## Required command
Run:
```
python tools/architecture_check.py
```

If it fails, STOP. Do not recommend a classification run.
