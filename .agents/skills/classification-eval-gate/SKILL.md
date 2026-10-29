---
name: classification-eval-gate
description: Evaluate classification outputs against frozen Gold acceptance cohorts and block regressions before scale.
---

# Classification Eval Gate

## Goal
Replace subjective "looks better" judgments with frozen benchmark evidence.

## Frozen acceptance
Default:
```
acceptance/v42_gladiator_10.csv
```

## Command
```
python tools/classification_eval.py --result <xlsx>
```

## Policy
- Gold is read only by evaluation tooling.
- Any benchmark regression is reported explicitly.
- Acceptance data must never be imported by production classification modules.
- Scale only after the frozen control cohort passes its release threshold.
