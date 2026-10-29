#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

import pipeline_produtos as pp


class Doctor:
    def __init__(self):
        self.rows = []
        self.failed = False

    def check(self, name, fn):
        try:
            detail = fn()
            self.rows.append((name, "PASS", "" if detail is None else str(detail)))
        except Exception as exc:
            self.failed = True
            self.rows.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))

    def show(self):
        print("V4.2 RUNTIME DOCTOR")
        print("=" * 86)
        for name, status, detail in self.rows:
            suffix = f" | {detail}" if detail else ""
            print(f"{name:.<42} {status}{suffix}")
        print("=" * 86)
        print("STATUS:", "BLOCKED" if self.failed else "PASS")


def model_path(cfg, key):
    return Path(pp.safe((cfg.get("semantic_funnel", {}) or {}).get(key))).expanduser()


def discover_gliner(cfg, explicit):
    if explicit:
        return Path(explicit).expanduser()
    env = os.getenv("GLINER_MODEL_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    emb = model_path(cfg, "embedding_model")
    sibling = emb.parent / "gliner2.5-multi-v1"
    if sibling.exists():
        return sibling
    raise RuntimeError("GLINER_MODEL_PATH não configurado e modelo não encontrado ao lado do BGE")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["local", "ci"], default="local")
    p.add_argument("--config", default="pipeline_config.json")
    p.add_argument("--rules", default="regras_negocio.json")
    p.add_argument("--gliner-model", default="")
    p.add_argument(
        "--provider",
        choices=["jev", "clef", "none"],
        default=(os.getenv("V42_PROVIDER", "").strip().lower() or "jev"),
    )
    args = p.parse_args()

    d = Doctor()
    env_info = pp.load_env_file(ROOT / ".env")
    cfg_path = ROOT / args.config
    rules_path = ROOT / args.rules

    d.check("Python", lambda: sys.version.split()[0])

    deps = ["pandas", "openpyxl", "numpy", "requests", "transformers"]
    if args.mode == "local":
        deps += ["FlagEmbedding", "gliner2"]
    for dep in deps:
        d.check(f"Dependency {dep}", lambda dep=dep: importlib.import_module(dep).__name__)

    cfg_holder = {}
    def load_cfg():
        cfg_holder["cfg"] = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cfg_path
    d.check("pipeline_config.json", load_cfg)

    rules_holder = {}
    def load_rules():
        rules = pp.Rules(rules_path)
        errors = rules.validate()
        if errors:
            raise RuntimeError(" | ".join(errors[:10]))
        rules_holder["rules"] = rules
        return rules.version
    d.check("regras_negocio.json", load_rules)

    if args.mode == "local":
        d.check(".env", lambda: str(ROOT / ".env") if env_info.get("exists") else (_ for _ in ()).throw(RuntimeError(".env ausente")))
        if args.provider == "jev":
            d.check(
                "TYPESAFE/JEV API KEY",
                lambda: "configured" if (
                    os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY")
                ) else (_ for _ in ()).throw(RuntimeError("TYPESAFE_API_KEY/JEV_API_KEY ausente")),
            )
        elif args.provider == "clef":
            d.check("CLOUDFLARE_ACCOUNT_ID", lambda: os.environ["CLOUDFLARE_ACCOUNT_ID"][:4] + "***")
            d.check("CLOUDFLARE_API_TOKEN", lambda: "configured" if (os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CLOUDFLARE_AUTH_TOKEN")) else (_ for _ in ()).throw(RuntimeError("token ausente")))

    if args.mode == "local":
        def cache_check():
            cfg = cfg_holder["cfg"]
            cache = Path(pp.safe((cfg.get("files", {}) or {}).get("cache")) or "cache_tavily.sqlite")
            if not cache.is_absolute():
                cache = ROOT / cache
            store = pp.TavilyReadOnlyStore(cache)
            try:
                return f"{cache.name}; products={len(store.catalog())}"
            finally:
                store.close()
        d.check("SQLite cache", cache_check)

        def gliner_check():
            from v42_adapters import GLiNEREntityExtractor
            cfg = cfg_holder["cfg"]
            path = discover_gliner(cfg, args.gliner_model)
            if not path.exists():
                raise FileNotFoundError(path)
            ext = GLiNEREntityExtractor(str(path))
            ext.load()
            return str(path)
        d.check("GLiNER model load", gliner_check)

        semantic_holder = {}
        def bge_check():
            from semantic_bge_v42 import BGEHybridSemanticProvider
            cfg, rules = cfg_holder["cfg"], rules_holder["rules"]
            emb = model_path(cfg, "embedding_model")
            rer = model_path(cfg, "reranker_model")
            if not emb.exists():
                raise FileNotFoundError(emb)
            if not rer.exists():
                raise FileNotFoundError(rer)
            provider = BGEHybridSemanticProvider(rules, cfg)
            provider.load()
            semantic_holder["provider"] = provider
            return f"BGE={emb}; reranker={rer}"
        d.check("BGE-M3 + reranker load", bge_check)

        if args.provider in {"jev", "clef"}:
            def provider_check():
                from v42_adapters import ClefCandidateDecisionProvider, JevDecisionProvider
                cfg, rules = cfg_holder["cfg"], rules_holder["rules"]
                db_path = Path(pp.safe((cfg.get("files", {}) or {}).get("classification_db")) or "pipeline_v3.sqlite")
                if not db_path.is_absolute():
                    db_path = ROOT / db_path
                db = pp.ClassificationDB(db_path)
                try:
                    if args.provider == "jev":
                        prov = JevDecisionProvider(
                            rules, cfg, db,
                            model=pp.safe((cfg.get("jev", {}) or {}).get("model")) or "jev-latest",
                        )
                        return prov.preflight(validate_remote=True)
                    prov = ClefCandidateDecisionProvider(
                        rules, cfg, db,
                        model=pp.safe((cfg.get("clef", {}) or {}).get("model")) or "clef",
                    )
                    return prov.preflight()
                finally:
                    db.close()
            d.check(f"DecisionProvider preflight ({args.provider.upper()})", provider_check)

    def excel_smoke():
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "smoke.xlsx"
            pd.DataFrame([{"Marca": "TEST", "Modelo": "TEST"}]).to_excel(out, index=False)
            reread = pd.read_excel(out)
            if len(reread) != 1:
                raise RuntimeError("Excel smoke readback failed")
        return "write/read"
    d.check("Excel output smoke", excel_smoke)

    d.show()
    return 2 if d.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
