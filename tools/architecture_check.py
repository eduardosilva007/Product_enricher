#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "classification_engine_v42.py",
    "semantic_bge_v42.py",
    "v42_adapters.py",
    "classify_v42.py",
    "regras_negocio.json",
    "pipeline_config.json",
]


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def class_node(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def main() -> int:
    failures = []
    for rel in REQUIRED:
        if not (ROOT / rel).exists():
            failures.append(f"MISSING_FILE:{rel}")

    if failures:
        for item in failures:
            print("FAIL", item)
        return 2

    eng = parse(ROOT / "classification_engine_v42.py")
    sem = parse(ROOT / "semantic_bge_v42.py")
    adapters = parse(ROOT / "v42_adapters.py")

    for cls in ["EntityExtractor", "SemanticProvider", "DecisionProvider"]:
        if class_node(eng, cls) is None:
            failures.append(f"MISSING_CONTRACT:{cls}")

    gliner = class_node(adapters, "GLiNEREntityExtractor")
    if gliner is None:
        failures.append("MISSING_GLINER_ENTITY_EXTRACTOR")
    else:
        returns_extracted = False
        forbidden_identifiers = {"canonical_type", "category_general", "family", "calibrated_close"}
        seen_identifiers = set()
        for node in ast.walk(gliner):
            if isinstance(node, ast.Name):
                seen_identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                seen_identifiers.add(node.attr)
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                if call_name(node.value) == "ExtractedEntities":
                    returns_extracted = True
                if call_name(node.value) in {"ProviderDecision", "SemanticDecision", "ClassificationResult"}:
                    failures.append("GLINER_RETURNS_DECISION_OBJECT")
        bad = sorted(forbidden_identifiers & seen_identifiers)
        if bad:
            failures.append("GLINER_DECISION_IDENTIFIERS:" + ",".join(bad))
        if not returns_extracted:
            failures.append("GLINER_DOES_NOT_RETURN_EXTRACTED_ENTITIES")

    bge = class_node(sem, "BGEHybridSemanticProvider")
    if bge is None:
        failures.append("MISSING_BGE_SEMANTIC_PROVIDER")
    else:
        found_false_gate = False
        for node in ast.walk(bge):
            if isinstance(node, ast.Call) and call_name(node) == "SemanticDecision":
                for kw in node.keywords:
                    if kw.arg == "calibrated_close" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                        found_false_gate = True
        if not found_false_gate:
            failures.append("BGE_LOCAL_CLOSE_NOT_EXPLICITLY_DISABLED")

    production_files = [
        "classification_engine_v42.py",
        "semantic_bge_v42.py",
        "v42_adapters.py",
        "classify_v42.py",
    ]
    for rel in production_files:
        text = (ROOT / rel).read_text(encoding="utf-8").casefold()
        if "acceptance/v42_" in text or "v42_gladiator_10" in text:
            failures.append(f"GOLD_LEAK_IN_RUNTIME:{rel}")

    if failures:
        print("ARCHITECTURE CHECK: BLOCKED")
        for item in failures:
            print(" -", item)
        return 2

    print("ARCHITECTURE CHECK: PASS")
    print(" - GLiNER extraction-only contract")
    print(" - BGE local auto-close disabled pending calibration")
    print(" - Provider interfaces separated")
    print(" - Acceptance/Gold not referenced by runtime modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
