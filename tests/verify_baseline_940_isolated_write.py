"""Offline full-persistence dry run of the failed 940 pilot (NO APIs).

Rebuilds temporary copies of the isolated Master and Final. Does not consume
the 20-SKU output and does not promote anything into production.

Usage:
 python tests/verify_baseline_940_isolated_write.py \
   piloto_argentina/controlled_night/20261112T190958Z_68f61e00
"""
import hashlib
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
    # The real classify/promotion persistence always receives the isolated
    # SQLite. Omitting it leaves stale legacy REVIEW_NO_RESULTS values that
    # cannot be reconciled even when RAW_OK evidence already exists.
    paths = {k: Path(cfg["files"][k]) for k in ("master", "final", "input", "cache")}
    if any(not p.is_file() for p in paths.values()):
        raise RuntimeError("BASELINE_PREFLIGHT_MISSING_ISOLATED_INPUT_OR_CACHE")
    def sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    source_hashes = {name: sha256(path) for name, path in paths.items()}
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
        local_cfg = dict(cfg)
        local_cfg["files"] = dict(cfg["files"])
        local_cfg["files"]["baseline_backup_dir"] = str(temp / "audit")
        stats = persist_baseline_excels(
            rows=[], master_path=master, final_path=final, input_path=cohort,
            rules=Rules(ROOT / "regras_negocio.json"),
            cache_path=paths["cache"], cfg=local_cfg,
        )
        after = load_baseline_inventory(master, final)
        lost = expected - after["processed"]
        final_lost = expected - after["final_accepted"]
        bi_after = _load_bi_lock_snapshot(final)
        bi_changed = {
            k for k, value in expected_bi.items()
            if bi_after.get(k) != value
        }
        original_changes = [
            name for name, path in paths.items()
            if sha256(path) != source_hashes[name]
        ]
        report = {
            "expected_accepted": len(expected),
            "accepted_after": len(after["processed"]),
            "lost_from_union": len(lost),
            "lost_from_final": len(final_lost),
            "bi_lock_changed": len(bi_changed),
            "bi_lock_count": len(expected_bi),
            "baseline_stats": stats,
            "api_calls": 0,
            "isolated_cache_readonly": str(paths["cache"]),
            "original_sources_modified": original_changes,
            "wrote_only_temporary_copies": True,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0 if not lost and not final_lost and not bi_changed and not original_changes else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/verify_baseline_940_isolated_write.py RUN_DIR")
    raise SystemExit(main(Path(sys.argv[1])))
