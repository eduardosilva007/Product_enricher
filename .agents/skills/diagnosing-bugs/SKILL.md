---
name: diagnosing-bugs
description: Reproduce Product Enricher failures with a deterministic red-green loop before changing production code.
---

# Diagnosing Bugs

Adapted engineering discipline from https://github.com/mattpocock/skills/tree/main/skills/engineering/diagnosing-bugs (not a verbatim copy).

1. Name the exact user-observable symptom and input, output, code version.
2. Build a fast deterministic command/test which exercises the actual failing interface (especially export, cache, classification and review statuses). Show it failing before modifying production behavior.
3. Minimize the reproducer; record 3 falsifiable ranked hypotheses without leaking secrets.
4. Instrument only boundaries needed to discriminate hypotheses. Mark temporary logs [DEBUG-incident-id].
5. Change a single variable; add an independent regression assertion and run red -> green.
6. Run original scenario, affected regression suite, release gate, and remove debug logs.

If no true reproduction can be constructed, record BLOCKED with missing artifacts. Do not claim a code fix is verified.
Never fabricate titles or accept wrong-SKU evidence simply to make a test green.
