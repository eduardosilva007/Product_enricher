"""CLI and menu route for a single canonical SQLite SKU merge."""
import inspect
import unittest
import pipeline_produtos as p


class SqliteSkuMergeEntrypointTests(unittest.TestCase):
    def test_cli_has_offline_sqlite_merge(self):
        args=p.build_parser().parse_args(["--mode","sqlite-sku-merge"])
        self.assertEqual(args.mode,"sqlite-sku-merge")
    def test_menu_points_to_sqlite_without_search(self):
        s=inspect.getsource(p.interactive_menu)
        self.assertIn("13 - UNIFICAR IDENTIDADES",s)
        self.assertIn('"--mode", "sqlite-sku-merge"',s)
    def test_entrypoint_is_dry_run_first_and_requires_gravar(self):
        s=inspect.getsource(p.main)
        self.assertIn('audit_sqlite_merge(final, master, db, keys, commit=False)',s)
        self.assertIn('audit_sqlite_merge(final, master, db, keys, commit=True)',s)
        self.assertIn('SQLITE_MERGE_NOT_COMMITTED',s)
        self.assertIn('len(keys) != 50',s)


if __name__=="__main__":
    unittest.main()
