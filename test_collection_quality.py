#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regressoes locais R9.5 Collection Quality. Nao chama Tavily nem LLM."""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCRIPT = BASE / "pipeline_produtos.py"
CONFIG = BASE / "pipeline_config.json"
POLICY = BASE / "collection_quality_policy.json"
RULES = BASE / "regras_negocio.json"


def load_module():
    spec = importlib.util.spec_from_file_location("pipeline_r95", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_r95"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m = load_module()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    cfg["_collection_policy"] = policy

    assert not m.validate_collection_policy(policy)

    # 1) Fonte dedicada.
    r = m._isolate_target_block(
        "TOTAL", "TL5508216", "TOTAL TL5508216 Electric Planer 550W",
        "https://example.test/TL5508216",
        "TOTAL TL5508216 Electric Planer 550W 82mm planing width", "", cfg, policy,
    )
    assert r["decision"] == "ACCEPT_TARGET_CLEAN", r

    # 2) Catalogo multi-produto: NAO descarta; isola somente a linha do target.
    r = m._isolate_target_block(
        "TOTAL", "TL5508216", "TOTAL Power Tools Catalogue",
        "https://example.test/catalog",
        "TL5508216 planer 550W\nTGSLI20126 vacuum cleaner 20V\nTDLI20012 drill 20V", "", cfg, policy,
    )
    assert r["decision"] == "ACCEPT_TARGET_ISOLATED", r
    assert "TL5508216" in r["content"] and "planer" in r["content"].lower(), r
    assert "TGSLI20126" not in r["content"] and "TDLI20012" not in r["content"], r
    assert "vacuum cleaner" not in r["content"].lower() and "drill" not in r["content"].lower(), r

    # 3) SKU apenas em compatibilidade nao define o produto principal.
    r = m._isolate_target_block(
        "TOTAL", "TGSLI20126", "Battery Pack for Cordless Tools",
        "https://example.test/battery",
        "Compatible with TGSLI20126 and other TOTAL tools", "", cfg, policy,
    )
    assert r["decision"] == "REJECT_COMPATIBILITY_ONLY", r

    # 4) SKU apenas em related products nao entra.
    r = m._isolate_target_block(
        "TOTAL", "TL5508216", "TOTAL Power Tools",
        "https://example.test/tools",
        "Related products: TL5508216 planer", "", cfg, policy,
    )
    assert r["decision"] == "REJECT_RELATED_ONLY", r

    # 5) Footer relacionado de uma pagina dedicada deve ser cortado.
    r = m._isolate_target_block(
        "TOTAL", "TL5508216", "TOTAL TL5508216 Electric Planer",
        "https://example.test/TL5508216",
        "TL5508216 Electric Planer 550W 82mm.\nRelated Products\nTGSLI20126 Vacuum Cleaner 20V", "", cfg, policy,
    )
    assert r["decision"] in {"ACCEPT_TARGET_CLEAN", "ACCEPT_TARGET_ISOLATED"}, r
    assert "TGSLI20126" not in r["content"], r

    # 6) _collection existente e vazio bloqueia fallback para RAW sujo.
    if RULES.exists():
        rules = m.Rules(RULES)
        assert not rules.validate()
        engine = m.DeterministicEngine(rules, cfg)
        payload = {
            "results": [{"title": "WRONG TGSLI20126 Vacuum Cleaner", "url": "x", "content": "TGSLI20126 vacuum cleaner 20V"}],
            "_collection": {"curated_results": []},
        }
        assert engine.build_sources("TOTAL", "TL5508216", payload) == []
        qg = m.run_quality_gates(rules, cfg)
        assert qg["synthetic_passed"] == qg["synthetic_cases"]
        assert qg["identity_passed"] == qg["identity_cases"]


    # 7) Fluxo Search -> Extract simulado: catalogo vira READY sem fallback extra.
    import os
    os.environ["TAVILY_API_KEY"] = "dummy"
    calls = []
    original_post = m._tavily_post_with_retry
    def fake_post(url, body, _cfg, _limiter):
        calls.append((url, dict(body)))
        if url.endswith("/search"):
            return {
                "results": [{
                    "title": "TOTAL Power Tools Catalogue",
                    "url": "https://catalog.test/all",
                    "content": "TL5508216 planer 550W [...] TGSLI20126 vacuum cleaner 20V",
                    "score": 0.8,
                }],
                "usage": {"credits": 1},
            }
        return {
            "results": [{
                "url": "https://catalog.test/all",
                "raw_content": "TL5508216 planer 550W\nTGSLI20126 vacuum cleaner 20V",
            }],
            "failed_results": [],
            "usage": {"credits": 1},
        }
    class NoWait:
        def wait(self):
            return 0
    m._tavily_post_with_retry = fake_post
    try:
        payload = m.tavily_search("TOTAL", "TL5508216", cfg, NoWait())
    finally:
        m._tavily_post_with_retry = original_post
    assert payload["_collection"]["state"] == "READY_FOR_CLASSIFICATION"
    assert len(payload["_collection"]["curated_results"]) == 1
    assert "TGSLI20126" not in payload["_collection"]["curated_results"][0]["content"]
    assert len([x for x in calls if x[0].endswith("/search")]) == 1


    # 8) Modelo operacional: especificacao final 220V nao faz parte do SKU de busca/identidade.
    assert m.derive_search_model("DONG CHENG", "DJG02 355 220V", cfg) == "DJG02 355"
    assert m.derive_search_model("DONG CHENG", "DMQ65K 220V", cfg) == "DMQ65K"
    assert m.derive_search_model("DONG CHENG", "DJG02 355 220 V", cfg) == "DJG02 355"
    # Tensao interna ao identificador nao deve ser removida.
    assert m.derive_search_model("BOSCH", "GSA 18V 32", cfg) == "GSA 18V 32"

    # 9) Evidencia explicita no titulo + SKU ancorado deve resolver deterministicamente.
    rules = m.Rules(RULES)
    engine = m.DeterministicEngine(rules, cfg)
    examples = [
        ("DJG02 355 220V", "Dongcheng DJG02-355 Electric Cut Off Machine at ₹ 6900", "CUT-OFF SAW"),
        ("DJG09 355 220V", "Dongcheng Electric Cut Off Machine DJG09-355", "CUT-OFF SAW"),
        ("DMQ65K 220V", "Dongcheng Jig Saw DMQ65K Price & Specifications", "JIGSAW"),
        ("DPL6 220V", "Dongcheng DPL6 P1L FF 6 EN Screwdriver", "SCREWDRIVER"),
    ]
    for model, title, expected_type in examples:
        rr = engine.classify("DONG CHENG", model, {"results": [{"title": title, "content": title, "url": "https://example.test/product"}]})
        assert rr.state == "RESOLVED_RULE", (model, rr.state, rr.type_candidates, rr.reasons)
        assert rr.product_type == expected_type, (model, rr.product_type, rr.type_candidates)

    # 10) Um titulo descritivo SEM SKU/URL/body target nao pode virar verdade apenas porque parece correto.
    rr = engine.classify("DONG CHENG", "DJF02 30 220V", {"results": [{
        "title": "Dongcheng 1300W Electric Reciprocating Saw",
        "content": "Dongcheng 1300W Electric Reciprocating Saw",
        "url": "https://example.test/generic",
    }]})
    assert rr.state == "UNRESOLVED" and not rr.type_candidates, rr

    # 11) O mesmo titulo passa quando o SKU e isolado no BODY/URL da mesma fonte.
    rr = engine.classify("DONG CHENG", "DJF02 30 220V", {"results": [{
        "title": "Dongcheng 1300W Electric Reciprocating Saw",
        "content": "Dongcheng DJF02-30 220V corded electric reciprocating saw",
        "url": "https://example.test/djf02-30",
    }]})
    assert rr.state == "RESOLVED_RULE" and rr.product_type == "RECIPROCATING SAW" and rr.power == "CORDED", rr

    # 12) BLOWER sem evidencia de alimentacao continua conservador; com 220V explicito resolve CORDED.
    rr = engine.classify("DONG CHENG", "DQF32", {"results": [{
        "title": "DongCheng Blower Vacuum 680W/1.6-3.2 m³/min DQF32",
        "content": "DongCheng Blower Vacuum 680W/1.6-3.2 m³/min DQF32",
        "url": "https://example.test/dqf32",
    }]})
    assert rr.product_type == "BLOWER" and rr.state == "AMBIGUOUS" and rr.power is None, rr
    rr = engine.classify("DONG CHENG", "DQF32", {"results": [{
        "title": "DongCheng Blower Vacuum 680W/1.6-3.2 m³/min DQF32",
        "content": "DongCheng DQF32 electric blower vacuum 220V 680W",
        "url": "https://example.test/dqf32",
    }]})
    assert rr.state == "RESOLVED_RULE" and rr.product_type == "BLOWER" and rr.power == "CORDED", rr


    # 13) Runtime R9.5 deve exigir camada _collection antes da classificacao.
    assert cfg.get("collection_quality", {}).get("require_curated_evidence_for_classification") is True
    raw = m.RawEvidence("RAW_OK", "TEST", "ABC123", payload={"results": [{"title": "ABC123 drill"}]})
    pending = m.record_for_uncurated_cache(raw)
    assert pending["Resolution State"] == "PENDING_CURATION"
    assert pending["Reason Code"] == "LEGACY_CACHE_NOT_CURATED"
    assert pending["Stage"] == "COLLECTION"

    print("R9.5 COLLECTION QUALITY TESTS: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
