#!/usr/bin/env python3
"""Offline safety tests for persistent, SHA-verified baseline inventory caching."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import baseline_inventory_cache as cache


class BaselineCacheSafety(unittest.TestCase):
    def test_second_read_hits_and_file_change_with_same_stat_invalidates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "base.xlsx"
            source.write_bytes(b"file-A")
            observed = []
            def load(p):
                observed.append(p.read_bytes())
                return ({"K1", "M"}, {"M"}, {"K1"})
            with patch.object(cache, "_CACHE_DIR", root / "cache"):
                a = cache.cached_baseline_keys(source, load, logic_digest="v1")
                self.assertEqual(len(observed), 1)
                a[0].clear()  # callers cannot poison the saved inventory
                b = cache.cached_baseline_keys(source, load, logic_digest="v1")
                self.assertEqual(b[0], {"K1", "M"})
                self.assertEqual(len(observed), 1)
                stat = source.stat()
                source.write_bytes(b"file-B")
                os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                cache.cached_baseline_keys(source, load, logic_digest="v1")
                self.assertEqual(len(observed), 2)
                cache.cached_baseline_keys(source, load, logic_digest="v2")
                self.assertEqual(len(observed), 3)

    def test_corrupt_cache_recomputes_and_never_returns_corrupt_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "base.xlsx"
            source.write_bytes(b"valid")
            observed = []
            def load(p):
                observed.append(True)
                return ({"K1"}, set(), set())
            with patch.object(cache, "_CACHE_DIR", root / "cache"):
                cache.cached_baseline_keys(source, load, logic_digest="v1")
                entry = cache._entry_path(source)
                entry.write_text("{broken", encoding="utf-8")
                result = cache.cached_baseline_keys(source, load, logic_digest="v1")
                self.assertEqual(result[0], {"K1"})
                self.assertEqual(len(observed), 2)


if __name__ == "__main__":
    unittest.main()
