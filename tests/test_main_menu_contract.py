"""Static menu regression: no provider calls and no canonical file writes."""
import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "pipeline_produtos.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
MENU = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "interactive_menu")
STRINGS = [n.value for n in ast.walk(MENU) if isinstance(n, ast.Constant) and isinstance(n.value, str)]

class MainMenuContract(unittest.TestCase):
    def test_five_top_level_actions(self):
        expected = ["1 - COLETAR PRODUTOS", "2 - CLASSIFICAR PRODUTOS",
                    "3 - PROCESSO COMPLETO", "4 - MANUTENCAO", "5 - SAIR"]
        for label in expected:
            self.assertTrue(any(s.startswith(label) for s in STRINGS), label)

    def test_all_legacy_modes_remain_reachable(self):
        expected = {
            "collect", "classify", "full", "recover", "repair", "curate",
            "evidence-export", "overnight-centralize", "final-schema-cleanup",
            "audit-central-cache", "final-lookup-index", "sqlite-sku-merge",
        }
        self.assertTrue(expected.issubset(set(STRINGS)))

    def test_maintenance_preserves_all_old_options(self):
        mappings = [node for node in ast.walk(MENU) if isinstance(node, ast.Dict)]
        found = False
        for node in mappings:
            try:
                d = ast.literal_eval(node)
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict) and d.get("9") == "13":
                self.assertEqual(set(d.values()), {"4", "5", "6", "7", "9", "10", "11", "12", "13"})
                found = True
        self.assertTrue(found, "Maintenance routes missing")

if __name__ == "__main__":
    unittest.main()
