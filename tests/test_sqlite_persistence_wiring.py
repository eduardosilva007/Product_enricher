"""Ensure registry refresh follows official Excel writes."""
import inspect
import unittest
import pipeline_produtos as p


class PersistenceIntegrationTests(unittest.TestCase):
    def test_regular_writer_ends_with_registry_refresh(self):
        src=inspect.getsource(p.persist_baseline_excels)
        self.assertLess(src.index("_export_base_final("),
                        src.index("_refresh_sqlite_sku_registry("))
        self.assertIn("sqlite_registry_status", src)

    def test_overnight_generic_stage_never_writes_canonical_sqlite(self):
        src=inspect.getsource(p.overnight_promote_campaign)
        self.assertIn('"classification_db": str(staged["classification_db"])', src)
        self.assertIn('cfg=staging_cfg,', src)
        self.assertLess(src.index("staging_cfg = {"),
                        src.index("stage_registry = _refresh_sqlite_sku_registry("))

    def test_overnight_registry_sync_is_pre_commit(self):
        src=inspect.getsource(p.overnight_promote_campaign)
        self.assertLess(src.index("stage_registry = _refresh_sqlite_sku_registry("),
                        src.index('        replaced = []'))
        self.assertIn('stats["sqlite_registry_campaign_keys"]', src)


if __name__ == "__main__":
    unittest.main()
