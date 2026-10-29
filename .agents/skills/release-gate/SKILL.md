---
name: release-gate
description: Orchestrate architecture, repository, tests, runtime, and acceptance checks and emit only BLOCKED, READY_FOR_CONTROL_RUN, or READY_FOR_SCALE.
---

# Release Gate

## Goal
The user is not the QA environment.

## Preflight
```
python tools/release_gate.py --phase preflight --mode local
```

Only acceptable success state:
```
READY_FOR_CONTROL_RUN
```

## Post control run
```
python tools/release_gate.py --phase postrun --mode local --result <xlsx>
```

Only acceptable scale state:
```
READY_FOR_SCALE
```

## Gate composition
- architecture check
- repository audit
- tests/contracts
- runtime doctor
- frozen acceptance (postrun)

Never tell the user to execute classification if the gate says BLOCKED.
Never equate CI green with runtime/model/provider verification.
