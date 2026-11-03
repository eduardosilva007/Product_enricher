"""Regressão: espelhamento de RAW no Consolidado sem inventar classificações."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pandas as pd

import pipeline_produtos as pipeline


class FakeStore:
    def __init__(self, mapping):
        self.mapping = mapping
        self.closed = False

    def get(self, brand, model):
        return self.mapping[(brand, model)]

    def close(self):
        self.closed = True


def input_item(brand, model):
    return SimpleNamespace(
        valid=True, key=pipeline.business_key(brand, model),
        brand_original=brand, model_original=model
    )


class CacheBaselineSyncTests(unittest.TestCase):
    def test_pending_raw_ok_and_raw_empty_and_gold(self):
        data = pd.DataFrame([
            {"Marca": "A", "Modelo": "1", "Revisão": "PENDENTE_COLETA", "Resolution State": "PENDING_COLLECTION"},
            {"Marca": "B", "Modelo": "2", "Revisão": "PENDENTE_COLETA", "Resolution State": "PENDING_COLLECTION"},
            {"Marca": "G", "Modelo": "3", "Revisão B.I": "OK", "Status": "Manual",
             "Revisão": "VALIDADO_MANUAL", "Resolution State": "RESOLVED", "Category General": "GOLD"},
        ], dtype="object").fillna("")
        mapping = {
            ("A", "1"): SimpleNamespace(state="RAW_OK", payload={
                "_collection": {"state": "READY_FOR_CLASSIFICATION"},
                "results": [{"title": "Produto A1", "url": "https://example.org/1"}],
            }),
            ("B", "2"): SimpleNamespace(state="RAW_EMPTY", payload={"results": []}),
        }
        store = FakeStore(mapping)
        with patch.object(pipeline, "read_input_rows",
                          return_value=[input_item("A", "1"), input_item("B", "2"), input_item("G", "3")]), \
             patch.object(pipeline, "TavilyReadOnlyStore", return_value=store), \
             patch.object(pipeline, "choose_reference_title",
                          return_value=("Produto A1", "https://example.org/1", "STRONG", "ACCEPT")), \
             patch.object(pipeline.Path, "exists", return_value=True):
            stats = pipeline._hydrate_input_collection_from_cache(
                data, pipeline.Path("fake-input.xlsx"), pipeline.Path("fake-cache.sqlite"), {}
            )
        self.assertEqual(stats["cache_raw_ok"], 1)
        self.assertEqual(stats["cache_raw_empty"], 1)
        self.assertEqual(data.loc[0, "Resolution State"], "PENDING_CLASSIFICATION")
        self.assertEqual(data.loc[0, "Revisão"], "PENDENTE_CLASSIFICACAO")
        self.assertEqual(data.loc[0, "Título Referência"], "Produto A1")
        self.assertEqual(data.loc[0, "Category General"], "")
        self.assertEqual(data.loc[1, "Collection State"], "RAW_EMPTY")
        self.assertEqual(data.loc[2, "Category General"], "GOLD")
        self.assertEqual(data.loc[2, "Revisão"], "VALIDADO_MANUAL")
        self.assertTrue(store.closed)
        self.assertEqual(pipeline._derive_final_review(data.loc[0], data), "PENDENTE_CLASSIFICACAO")

    def test_export_review_distinguishes_collection_from_classification(self):
        df = pd.DataFrame([
            {"Marca": "A", "Modelo": "1", "Raw State": "RAW_OK",
             "Collection State": "READY_FOR_CLASSIFICATION",
             "Resolution State": "NEEDS_RECOLLECTION", "Revisão": "PENDENTE_COLETA"},
            {"Marca": "B", "Modelo": "2", "Raw State": "RAW_OK",
             "Collection State": "NO_TARGET_EVIDENCE",
             "Resolution State": "NEEDS_RECOLLECTION", "Revisão": "PENDENTE_COLETA"},
            {"Marca": "C", "Modelo": "3", "Raw State": "RAW_EMPTY",
             "Collection State": "RAW_EMPTY",
             "Resolution State": "PENDING_RECOLLECTION", "Revisão": "PENDENTE_COLETA"},
        ], dtype="object")
        self.assertEqual(pipeline._derive_final_review(df.loc[0], df), "PENDENTE_RECOLETA")
        self.assertEqual(pipeline._derive_final_review(df.loc[1], df), "PENDENTE_RECOLETA")
        self.assertEqual(pipeline._derive_final_review(df.loc[2], df), "PENDENTE_RECOLETA")

    def test_500_pending_cache_rows_idempotent(self):
        n = 500
        brands = ["STRESS"] * n
        models = [f"M{i:04d}" for i in range(n)]
        data = pd.DataFrame({
            "Marca": brands, "Modelo": models,
            "Revisão": ["PENDENTE_COLETA"] * n,
            "Resolution State": ["PENDING_COLLECTION"] * n,
        }, dtype="object")
        items = [input_item(b, m) for b, m in zip(brands, models)]
        mapping = {(b, m): SimpleNamespace(state="RAW_OK", payload={
            "_collection": {"state": "READY_FOR_CLASSIFICATION"},
            "results": [],
        }) for b, m in zip(brands, models)}
        with patch.object(pipeline, "read_input_rows", return_value=items), \
             patch.object(pipeline, "TavilyReadOnlyStore", side_effect=lambda _: FakeStore(mapping)), \
             patch.object(pipeline.Path, "exists", return_value=True):
            first = pipeline._hydrate_input_collection_from_cache(
                data, pipeline.Path("fake-input.xlsx"), pipeline.Path("fake-cache.sqlite"), {}
            )
            second = pipeline._hydrate_input_collection_from_cache(
                data, pipeline.Path("fake-input.xlsx"), pipeline.Path("fake-cache.sqlite"), {}
            )
        self.assertEqual(first["cache_raw_ok"], n)
        self.assertEqual(second["cache_raw_ok"], n)
        self.assertTrue((data["Revisão"] == "PENDENTE_CLASSIFICACAO").all())
        self.assertTrue((data["Resolution State"] == "PENDING_CLASSIFICATION").all())


if __name__ == "__main__":
    unittest.main()
