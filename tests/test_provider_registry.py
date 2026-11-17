import unittest
from provider_registry import (
    PROVIDERS, manual_choices, automatic_choices, validate_registry,
)

class RegistryTests(unittest.TestCase):
    def test_six_registered(self):
        self.assertEqual(
            {p.key for p in PROVIDERS},
            {"tavily", "serper", "brightdata", "scraperapi", "octoparse", "openserp"},
        )

    def test_only_real_production_providers(self):
        expected = {"tavily", "serper", "brightdata"}
        self.assertEqual({p.key for p in manual_choices()}, expected)
        self.assertEqual({p.key for p in automatic_choices()}, expected)
        validate_registry(expected)

    def test_experimental_not_routable(self):
        self.assertFalse(
            any(p.manual or p.automatic for p in PROVIDERS if p.status == "LABORATORIO")
        )

if __name__ == "__main__":
    unittest.main()
