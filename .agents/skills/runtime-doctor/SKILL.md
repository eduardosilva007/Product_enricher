---
name: runtime-doctor
description: Run local runtime preflight checks for env, dependencies, cache, models, providers, and output generation before classification.
---

# Runtime Doctor

## Goal
Ensure the user never discovers basic integration failures during classification.

## Local preflight
Run:
```
python tools/runtime_doctor.py --mode local
```

It must verify:
- Python/dependencies
- repository .env loading
- config/rules
- cache SQLite
- GLiNER path and actual model load
- BGE-M3 path and actual model load
- reranker path and actual model load
- DecisionProvider preflight
- Excel output smoke test

## Policy
A failed runtime check blocks classification.
CI success does not imply local runtime success.
