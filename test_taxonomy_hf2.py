#!/usr/bin/env python3
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pipeline_produtos", ROOT / "pipeline_produtos.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["pipeline_produtos"] = mod
spec.loader.exec_module(mod)

rules = mod.Rules(ROOT / "regras_negocio.json")
errors = rules.validate()
if errors:
    raise SystemExit("RULES INVALID:\n" + "\n".join(errors))

cfg = mod.read_json(ROOT / "pipeline_config.json")
stats = mod.run_quality_gates(rules, cfg)
assert stats["synthetic_passed"] == stats["synthetic_cases"]
assert stats["identity_passed"] == stats["identity_cases"]
assert stats["real_passed"] == stats["real_explicit"]

assert ("WATERPUMPS", "WATERPUMP") not in rules.valid_pairs
print("R9.5 HF2 TAXONOMY TESTS: OK")
print(stats)
