"""SKU-specific auto-approval regressions for overnight campaigns.

Offline only: does not query providers, write production Excel or spend tokens.
"""
import unittest

from pipeline_produtos import _overnight_auto_title_identity_errors

CONFIG = {"identity": {"regional_suffixes": []}}


def candidate(model, title, *, auto="SIM"):
    return {
        "Marca": "KSB",
        "Modelo": model,
        "Título Real": title,
        "Auto Eligible": auto,
        "Resolution State": "RESOLVED",
        "Review Required": "NÃO",
        "O que é (Tipo)": "PUMP",
        "Category General": "OTHERS",
        "FAMILY": "CENTRIFUGAL PUMP",
        "Classification Identity": "STRONG",
    }


class OvernightKSBIdentityGateTests(unittest.TestCase):
    def test_wrong_variant_cannot_be_automatically_approved(self):
        row = candidate("MTC A32/5A", "KSB MTC A 32/7C multistage pump")
        self.assertIn(
            "AUTO_TITLE_IDENTITY_MISMATCH",
            _overnight_auto_title_identity_errors(row, CONFIG),
        )

    def test_exact_variant_with_separator_variation_is_allowed(self):
        row = candidate("MTC A32/5A", "KSB MTC A 32/5A multistage pump")
        self.assertEqual(_overnight_auto_title_identity_errors(row, CONFIG), [])

    def test_generic_multitec_series_not_sku_evidence(self):
        for model in ("MULTITEC A32/14A/2.1", "MULTITEC A32/8A/2.1"):
            with self.subTest(model=model):
                row = candidate(model, "KSB MULTITEC BH multistage pump")
                self.assertIn(
                    "AUTO_TITLE_IDENTITY_MISMATCH",
                    _overnight_auto_title_identity_errors(row, CONFIG),
                )

    def test_generic_upa_series_not_sku_evidence(self):
        row = candidate("UPA 250/150/03GB", "KSB UPA 250 submersible pump")
        self.assertIn(
            "AUTO_TITLE_IDENTITY_MISMATCH",
            _overnight_auto_title_identity_errors(row, CONFIG),
        )

    def test_partial_slash_identifier_is_not_exact_sku(self):
        row = candidate("MTC A32/5A", "KSB MTC A32/7C multistage pump")
        self.assertEqual(
            _overnight_auto_title_identity_errors(row, CONFIG),
            ["AUTO_TITLE_IDENTITY_MISMATCH"],
        )

    def test_full_upa_identifier_can_pass_title_identity(self):
        row = candidate("UPA 250/150/03GB", "KSB UPA 250/150/03GB pump")
        self.assertEqual(_overnight_auto_title_identity_errors(row, CONFIG), [])

    def test_missing_title_is_not_approved(self):
        row = candidate("MTC A32/5A", "")
        self.assertEqual(
            _overnight_auto_title_identity_errors(row, CONFIG),
            ["AUTO_TITLE_MISSING_TARGET_OR_EVIDENCE"],
        )

    def test_proposal_requiring_review_stays_unapproved(self):
        row = candidate("MTC A32/5A", "KSB MTC A 32/7C", auto="NÃO")
        row["Review Required"] = "SIM"
        self.assertEqual(_overnight_auto_title_identity_errors(row, CONFIG), [])


if __name__ == "__main__":
    unittest.main()
