---
name: tdd
description: Use test-first vertical slices on Product Enricher public interfaces, especially exports, identity and taxonomy.
---

# TDD

Adapted from https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd.

- Pick the observable seam and acceptance example first. Prefer pipeline entrypoint, exported workbook, cache adapter or classifier public interface over mocking private helper internals.
- Use an independent known expected result from specification/frozen Gold, not the implementation under test.
- Run the test and capture the RED failure for the exact reported bug.
- Implement only the minimal change for GREEN, then rerun the original symptom.
- Protect manual-review integrity: preserved rows, titles, evidence, URLs, unresolved status, and no accidental taxonomy approvals.
- Keep Gold isolated from production paths. Do not encode Brand/Model-specific exceptions.
- Run targeted tests, all tests and release-gate preflight before handoff.
