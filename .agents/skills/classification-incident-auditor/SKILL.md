---
name: classification-incident-auditor
description: Diagnose failures by layer, reproduce the exact symptom, and require regression tests before any rules/alias change.
---

# Classification Incident Auditor

Read GLOSSARY.md and invoke diagnosing-bugs procedure before modifying code.

Command:
    python tools/incident_auditor.py --result <xlsx>

Classification classes:
IDENTITY_ERROR, EVIDENCE_ERROR, ROLE_ERROR, RULE_ERROR,
ENTITY_EXTRACTION_ERROR, SEMANTIC_RETRIEVAL_ERROR, RERANK_ERROR,
DECISION_PROVIDER_ERROR, TAXONOMY_MAPPING_ERROR, OPERATIONAL_ERROR,
OUTPUT_INTEGRITY_ERROR.

For every incident:
1. Capture a minimal input/product plus source data, redacting secrets.
2. Reproduce the actual failing public boundary; test must fail on the observed symptom.
3. Rank falsifiable hypotheses and identify the faulty layer.
4. Fix the class of failure, never a benchmark-specific SKU exception.
5. Run the new regression and original scenario; attach output comparison.

If no red-capable repro exists, mark diagnosis unverified, not fixed.
