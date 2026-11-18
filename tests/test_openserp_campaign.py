"""Offline-only acceptance tests for OpenSERP campaign safety."""
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import openserp_campaign as campaign


def artifact(skus):
    summary = {"results": [
        {"provider": "openserp", "query": f"{s['brand']} {s['model']}",
         "status": "CAPTURED", "organic": [
             {"type": "organic", "title": "example",
              "url": "https://example.com", "snippet": ""}]}
        for s in skus]}
    manifest = io.StringIO()
    writer = csv.DictWriter(manifest, fieldnames=["query", "brand", "model"])
    writer.writeheader()
    for sku in skus:
        writer.writerow({"query": f"{sku['brand']} {sku['model']}", **sku})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("pilot_summary.json", json.dumps(summary))
        z.writestr("openserp_manifest.csv", manifest.getvalue())
        z.writestr("manifest.json", '{"engine":"google"}')
    return data.getvalue()


class CampaignSafetyTests(unittest.TestCase):
    def test_multiword_brand_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.csv"
            p.write_text("brand,model\nBRIGGS & STRATTON,40U7770010\n", encoding="utf-8")
            self.assertEqual(campaign.read_skus(p)[0]["brand"], "BRIGGS & STRATTON")
            p.write_text("brand,model\nSTIHL,FS250\nSTIHL,FS-250\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DUPLICATE"):
                campaign.read_skus(p)

    def test_zip_accepts_exact_skus(self):
        skus = [{"brand": "STIHL", "model": "FS250"},
                {"brand": "BRIGGS & STRATTON", "model": "40U7770010"}]
        with tempfile.TemporaryDirectory() as d:
            digest = campaign.unpack_checked(artifact(skus), Path(d), skus)
            self.assertEqual(len(digest), 64)
            self.assertTrue((Path(d) / "pilot_summary.json").is_file())

    def test_zip_rejects_wrong_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "WRONG_SKU"):
                campaign.unpack_checked(
                    artifact([{"brand": "STIHL", "model": "MS382"}]), Path(d),
                    [{"brand": "STIHL", "model": "FS250"}])

    def test_zip_rejects_traversal(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../pilot_summary.json", "{}")
            z.writestr("openserp_manifest.csv", "query,brand,model")
            z.writestr("manifest.json", "{}")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "UNSAFE_ARTIFACT"):
                campaign.unpack_checked(buf.getvalue(), Path(d), [])

    def test_wait_failure_fails_closed(self):
        with patch.object(campaign, "find_run", return_value={
            "id": 1234, "status": "completed", "conclusion": "failure"}):
            with self.assertRaisesRegex(RuntimeError, "NOT_SUCCESS"):
                campaign.wait_run("dummy", "unique", 20)

    def test_find_run_exact_nonce(self):
        with patch.object(campaign, "request", return_value={
            "workflow_runs": [
                {"id": 1, "display_title": "openserp-wrong",
                 "head_branch": campaign.BRANCH, "event": "workflow_dispatch"},
                {"id": 2, "display_title": "openserp-correct",
                 "head_branch": campaign.BRANCH, "event": "workflow_dispatch"}]}):
            self.assertEqual(campaign.find_run("dummy", "correct")["id"], 2)


if __name__ == "__main__":
    unittest.main()
