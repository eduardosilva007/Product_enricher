---
name: change-impact-analyzer
description: Analyze changed files, affected runtime layers, required tests, and regression risk before release.
---

# Change Impact Analyzer

## Goal
Prevent local fixes from breaking downstream modules.

## Required command
```
python tools/change_impact.py --base origin/main
```

## Analyze
- changed files
- affected runtime layers
- interface/config/output changes
- required checks
- risk level

High-risk changes include:
- identity
- classification engine
- semantic provider
- entity extractor
- DecisionProvider
- rules/config schema
- output schema

Do not approve a high-risk change based on unit tests alone.
