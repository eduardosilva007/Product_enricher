#!/usr/bin/env python3
"""Offline regression tests: fail-closed source snapshots and exact Final row coverage."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openpyxl import Workbook
import input_sync_manifest as manifest
from final_cohort_coverage import audit_final_cohort


class SyncAndCoverage(unittest.TestCase):
    def test_sync_manifest_is_strict_and_invalidated_by_content(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            inputs = {}
            for name in ("input", "master", "final", "cache", "rules", "config"):
                p = folder / (name+".data")
                p.write_bytes(("value:"+name).encode())
                inputs[name] = p
            marker = folder / "snapshot.json"
            self.assertFalse(manifest.is_current(inputs, marker))
            manifest.record_success(inputs, marker)
            self.assertTrue(manifest.is_current(inputs, marker))
            st = inputs["final"].stat()
            inputs["final"].write_bytes(b"changedfinal")
            os.utime(inputs["final"], ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertFalse(manifest.is_current(inputs, marker))
            manifest.record_success(inputs, marker)
            self.assertTrue(manifest.is_current(inputs, marker))
            inputs["cache"].with_name(inputs["cache"].name+"-wal").write_bytes(b"uncheckpointed")
            self.assertFalse(manifest.is_current(inputs, marker))
            manifest.record_success(inputs, marker)
            self.assertTrue(manifest.is_current(inputs, marker))
            inputs["cache"].with_name(inputs["cache"].name+"-wal").write_bytes(b"other")
            self.assertFalse(manifest.is_current(inputs, marker))
            marker.write_text("{bad json", encoding="utf-8")
            self.assertFalse(manifest.is_current(inputs, marker))

    def test_row_coverage_checks_physical_exact_identity_not_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "final.xlsx"
            book = Workbook()
            ws = book.active
            ws.title = "Base Final"
            ws.append(["Marca", "Modelo", "Category General", "FAMILY"])
            ws.append(["KRENN", "OS /16C", None, None])
            ws.append(["KRENZ", "F26D", "OTHERS", "COOLING FAN"])
            book.save(final)
            cohort = [
                {"Marca": "KRENN", "Modelo": "OS /16C"},
                {"Marca": "KRENZ", "Modelo": "F26D"},
            ]
            result = audit_final_cohort(final, cohort)
            self.assertEqual((result["present"],result["missing"]),(2,0))
            with self.assertRaisesRegex(RuntimeError,"FULL_FINAL_ROW_COVERAGE_FAILED"):
                audit_final_cohort(final, cohort+[{"Marca":"KRENZ","Modelo":"NOT PRESENT"}])
            # Same normalized business key / different physical SKU is NOT coverage.
            with self.assertRaisesRegex(RuntimeError,"FULL_FINAL_ROW_COVERAGE_FAILED"):
                audit_final_cohort(final, [{"Marca":"KRENN","Modelo":"OS/16C"}])
            with self.assertRaisesRegex(RuntimeError,"FULL_FINAL_WORKBOOK_MISSING"):
                audit_final_cohort(final.with_name("missing.xlsx"), cohort)


if __name__ == "__main__":
    unittest.main()
