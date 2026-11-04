---
name: code-review
description: Review Product Enricher diffs for both engineering quality and functional specification compliance.
---

# Two-Axis Code Review

Adapted from https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review.

Compare against an explicit merge base (usually origin/feature/v42-classification-engine for this branch). Do not approve an empty or unresolved diff.

## Axis A — Standards
Inspect coupling, duplicate logic, secret handling, error states, local-only constraints, test determinism, unnecessary changes, provider call limits, cache contracts and architecture-guardian checks. Cite file and line.

## Axis B — Functional specification
Check preservation of each input row, useful observed reference title, source URL and evidence for manual review; correct identity and rejection decisions; Category General/FAMILY taxonomy; hard locks; Gold isolation; no silent changes in schema and behavior. Cite requirement and affected file.

Report both independently; include failing tests, untested paths, and release risk. No claim of READY_FOR_SCALE without postrun workbook integrity and frozen acceptance.
