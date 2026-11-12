"""Offline full-persistence dry run of the failed 940 pilot (NO APIs).

Rebuilds temporary copies of the isolated Master and Final. Does not consume
the 20-SKU output and does not promote anything into production.

Usage:
 python tests/verify_baseline_940_isolated_write.py \
   piloto_argentina/controlled_night/20261112T190958Z_68f61e00
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline_produtos import (
    Rules, load_baseline_inventory, persist_baseline_excels,
    _load_bi_lock_snapshot,
)


def main(run_dir: Path):
    run_dir = run_dir.resolve()
    cfg = json.loads((run_dir / "isolated_config.json").read_text(encoding="utf-8"))
    paths = {k: Path(cfg["files"][k]) for k in ("master", "final", "input")}
    expected = load_baseline_inventory(paths["master"], paths["final"])["processed"]
    expected_bi = _load_bi_lock_snapshot(paths["final"])
    with tempfile.TemporaryDirectory(prefix="baseline_940_offline_") as temp_dir:
        temp = Path(temp_dir)
        master = temp / ("master" + paths["master"].suffix)
        final = temp / ("final" + paths["final"].suffix)
        cohort = temp / ("input" + paths["input"].suffix)
        for key, dest in (("master", master), ("final", final), ("input", cohort)):
            shutil.copy2(paths[key], dest)
        before = load_baseline_inventory(master, final)
        stats = persist_baseline_excels(
            rows=[], master_path=master, final_path=final, input_path=cohort,
            rules=Rules(ROOT / "regras_negocio.json"),
            cache_path=None, cfg=None,
        )
        after = load_baseline_inventory(master, final)
        lost = expected - after["processed"]
        final_lost = expected - after["final_accepted"]
        bi_after = _load_bi_lock_snapshot(final)
        bi_changed = {
            k for k, value in expected_bi.items()
            if bi_after.get(k) != value
        }
        report = {
            "expected_accepted": len(expected),
            "accepted_after": len(after["processed"]),
            "lost_from_union": len(lost),
            "lost_from_final": len(final_lost),
            "bi_lock_changed": len(bi_changed),
            "bi_lock_count": len(expected_bi),
            "baseline_stats": stats,
            "api_calls": 0,
            "wrote_only_temporary_copies": True,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0 if not lost and not final_lost and not bi_changed else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/verify_baseline_940_isolated_write.py RUN_DIR")
    raise SystemExit(main(Path(sys.argv[1])))
