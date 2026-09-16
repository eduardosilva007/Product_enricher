#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regressoes HF3: title rescue, taxonomy consensus e pump series. Sem Tavily/LLM."""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location("pipeline_hf3", BASE / "pipeline_produtos.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_hf3"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m = load_module()
    cfg = json.loads((BASE / "pipeline_config.json").read_text(encoding="utf-8"))
    policy = json.loads((BASE / "collection_quality_policy.json").read_text(encoding="utf-8"))
    cfg["_collection_policy"] = policy
    rules = m.Rules(BASE / "regras_negocio.json")
    assert not rules.validate(), rules.validate()
    engine = m.DeterministicEngine(rules, cfg)

    def raw(brand: str, model: str, title: str):
        payload = {
            "results": [{"title": title, "url": "https://example.test/item", "content": "", "score": 0.9}],
            "_collection": {
                "version": policy.get("VERSION", "R9.5"),
                "state": "NO_TARGET_EVIDENCE",
                "curated_results": [],
                "source_audit": [],
            },
        }
        return m.RawEvidence("RAW_OK", brand, model, payload=payload)

    # 1) Extract falhou, mas Search title prova Marca+SKU+TYPE.
    r = raw("DONG CHENG", "DJG02 355 220V", "Dongcheng DJG02-355 Electric Cut Off Machine at ₹ 6900")
    s = engine.classify(r.brand, r.model, r.payload)
    assert s.state == "RESOLVED_RULE", s
    assert s.product_type == "CUT-OFF SAW", s
    assert any(x.collection_decision == "ACCEPT_TITLE_ONLY_RESCUE" for x in s.sources), s.sources

    # 2) Coincidencia numerica sem marca nao pode virar evidencia automatica.
    r = raw("RONIX", "8600C", "8600C - White Inspectors Lisle Cotton Gloves")
    s = engine.classify(r.brand, r.model, r.payload)
    assert s.state == "UNRESOLVED", s
    assert not s.sources, s.sources
    ref = m.choose_reference_title(r, r.brand, r.model, cfg)
    assert ref[0] and ref[2] == "WEAK", ref

    # 3) Taladro Atornillador e um drill-driver, nao dois produtos soltos.
    r = raw("PEKTRA", "PKCD1210CK", "Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v")
    s = engine.classify(r.brand, r.model, r.payload)
    assert s.state == "RESOLVED_RULE", s
    assert s.product_type == "DRILL", s
    assert s.power == "BATTERY", s
    assert s.taxonomy[:2] == ("OTHERS", "POWER TOOLS"), s.taxonomy
    assert "SCREWDRIVER" not in s.type_candidates, s.type_candidates

    # 4) Se TYPE continuar ambiguo, Category/FAMILY pode ser consenso sem auto-aprovar TYPE.
    payload = {
        "results": [{"title": "Echo DM-6120LA Duster/Mister", "url": "https://example.test/dm6120la", "content": "", "score": 0.9}],
        "_collection": {
            "version": policy.get("VERSION", "R9.5"),
            "state": "READY_FOR_CLASSIFICATION",
            "curated_results": [{
                "title": "Echo DM-6120LA Duster/Mister",
                "url": "https://example.test/dm6120la",
                "content": "Echo DM-6120LA Duster/Mister",
                "score": 0.9,
                "_collection_decision": "ACCEPT_TARGET_CLEAN",
            }],
            "source_audit": [],
        },
    }
    raw_ev = m.RawEvidence("RAW_OK", "ECHO", "DM 6120LA", payload=payload)
    s = engine.classify("ECHO", "DM 6120LA", payload)
    rec = m.finalize_product("ECHO", "DM 6120LA", raw_ev, s, rules, cfg, None, None, "test")
    assert rec["Review Required"] == "SIM", rec
    assert rec["Category General"] == "MISTER/DUSTER", rec
    assert rec["FAMILY"] == "MISTER/DUSTER", rec

    # 5) Series de bombas ja validadas manualmente nao ficam sem taxonomia.
    cases = [
        ("LOWARA", "1016LC141", "WATERPUMP", "Lowara SV-F 1SV15F007T/D Multistage Pump", "CENTRIFUGAL PUMP"),
        ("WILO", "MEDANA CH1 L.403 1/E", "WATERPUMP", "Medana CH1-L.403-1/E/E/10T | Multistage monobloc pump", "CENTRIFUGAL PUMP"),
        ("SHIMGE", "PW125Z", "LIFT PUMP", "Shimge PW125Z Surface Automatic Booster Pump", "PERIPHERAL PUMP"),
    ]
    for brand, model, typ, title, expected_family in cases:
        cg, fam, _ = rules.map_taxonomy(typ, "UNKNOWN", title, brand, model)
        assert cg == "OTHERS" and fam == expected_family, (brand, model, cg, fam)

    print("R9.5 HF3 RECOVERY TESTS: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
