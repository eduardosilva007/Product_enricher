---
name: classification-eval-gate
description: Evaluate classifications against independent frozen Gold, fail on malformed cohorts, and report by FAMILY.
---

# Classification Evaluation Gate

Read docs/benchmark_protocol.md before evaluating or tuning.
The ten-item Gladiator cohort is a regression smoke test, not population accuracy.

Run:
    python tools/classification_eval.py --result <xlsx> --gold acceptance/v42_gladiator_10.csv --report-json reports/eval.json

Mandatory:
- Gold is independently reviewed and evaluation-only, never in classifier inputs or prompts.
- Empty Gold and output, duplicate keys, and missing Gold labels block evaluation.
- Report exact Category General + FAMILY pair agreement and per-FAMILY errors.
- Split training/validation/test by SKU model family, not row randomization; no near-variant leakage.
- Thresholds selected on validation; held-out test is not tuned against.
- A postrun must also validate input and final Excel integrity before scale.
- Classification failures use diagnosing-bugs and TDD before any corrective changes.
