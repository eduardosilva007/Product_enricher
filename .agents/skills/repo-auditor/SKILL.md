# Repository Auditor

## Goal
Audit the complete execution path before changing classification behavior.

## Required audit path
entrypoint -> config -> env -> dependencies -> cache -> engine -> adapters -> output -> tests -> CI

## Required command
```
python tools/repo_audit.py
```

## Rules
- Inspect all call sites when an interface or constructor changes.
- Check .env loading in every executable entrypoint that uses providers.
- Check config/rules files parse before runtime.
- Check secrets are not tracked.
- Check runtime modules compile.
- Report working-tree changes; do not silently delete or reset user files.
