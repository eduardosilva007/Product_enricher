---
name: classification-incident-auditor
description: Diagnose classification failures by layer before any rule or alias change is attempted.
---

# Classification Incident Auditor

## Goal
Diagnose the failing layer before changing rules or aliases.

## Command
```
python tools/incident_auditor.py --result <xlsx>
```

## Incident classes
- IDENTITY_ERROR
- EVIDENCE_ERROR
- ROLE_ERROR
- RULE_ERROR
- ENTITY_EXTRACTION_ERROR
- SEMANTIC_RETRIEVAL_ERROR
- RERANK_ERROR
- DECISION_PROVIDER_ERROR
- TAXONOMY_MAPPING_ERROR
- OPERATIONAL_ERROR

## Policy
Never fix a failing benchmark row with a SKU-specific production condition.
Fix the class of failure and add a regression test covering the general behavior.
