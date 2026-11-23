"""Ensure nightly human-review contract is enforced at the CLI and commit gate."""
import ast
from pathlib import Path
import unittest

SRC=Path(__file__).resolve().parents[1]/"pipeline_produtos.py"

class OvernightReviewBeforePromotionTest(unittest.TestCase):
    def test_review_flag_default_off_and_opt_in(self):
        from pipeline_produtos import build_parser
        parser=build_parser()
        original=parser.parse_args(["--mode","overnight"])
        safe=parser.parse_args(["--mode","overnight","--campaign-review-before-promotion"])
        self.assertFalse(original.campaign_review_before_promotion)
        self.assertTrue(safe.campaign_review_before_promotion)

    def test_campaign_never_promotes_by_itself(self):
        """Review gate is mandatory (OVERNIGHT_REVIEW_GATE_REQUIRED), so the
        campaign loop must contain NO call to overnight_promote_campaign:
        promotion is always a separate, explicit, post-review step."""
        tree=ast.parse(SRC.read_text(encoding="utf-8-sig"))
        campaign=next(node for node in tree.body
                      if isinstance(node,ast.FunctionDef) and node.name=="overnight_campaign")
        promote_calls=[node for node in ast.walk(campaign)
                       if isinstance(node,ast.Call)
                       and isinstance(node.func,ast.Name)
                       and node.func.id=="overnight_promote_campaign"]
        self.assertEqual(promote_calls,[])
        self.assertIn("OVERNIGHT_REVIEW_GATE_REQUIRED",ast.unparse(campaign))

if __name__=="__main__":
    unittest.main()
