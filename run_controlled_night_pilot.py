"""Legacy compatibility entrypoint.

The operational overnight engine now lives inside pipeline_produtos.py.
New execution: python pipeline_produtos.py --mode overnight ...
This thin shim remains only for scripts and tests using historical imports.
"""
import sys
from pipeline_produtos import (
    ROOT, OUT, gate, create_run, source_paths, snapshot, digest, atomic_json,
    import_prior_reservations, protected_originals, run_stage, outcome_audit,
    exclusive_campaign_lock, overnight_run_one, overnight_campaign,
)

def main():
    from pipeline_produtos import build_parser, read_json
    from pathlib import Path
    argv = list(sys.argv[1:])
    if "--mode" not in argv:
        argv = ["--mode", "overnight"] + argv
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    cfg = read_json(cfg_path)
    with exclusive_campaign_lock():
        return overnight_campaign(args, cfg)

if __name__ == "__main__":
    raise SystemExit(main())
