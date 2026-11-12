"""Night readiness rules for safe, scalable one-request-per-SKU policy."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location(
    "audit_night_readiness",Path(__file__).resolve().parents[1]/"scripts"/"audit_night_readiness.py")
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class NightReadinessRules(unittest.TestCase):
    def test_raw_empty_marked_does_not_become_retry(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_EMPTY","REQUEST_ATTEMPTED_NO_AUTO_RETRY",False,False,False,False),
            "RAW_EMPTY_COM_MARCADOR_TENTATIVA")
    def test_raw_empty_without_marker_stays_unproven(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_EMPTY","",False,False,False,False),
            "RAW_EMPTY_SEM_COMPROVACAO_TENTATIVA")
    def test_reserved_empty_is_never_new(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_EMPTY","",False,True,False,False),
            "RAW_EMPTY_JA_RESERVADO")
    def test_raw_missing_new_is_distinct(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_MISSING","",False,False,False,False),
            "NOVA_COLETA_POTENCIAL")
    def test_raw_missing_reserved_blocked(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_MISSING","",False,True,False,False),
            "RAW_MISSING_JA_RESERVADO")
    def test_raw_ok_goes_to_cache(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_OK","READY_FOR_CLASSIFICATION",False,False,False,False),
            "REAPROVEITAR_CACHE")
    def test_protected_wins_even_when_raw_empty(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_EMPTY","REQUEST_ERROR",True,True,True,True),
            "PRESERVAR_HUMANO")
    def test_unaccepted_but_processed_protected(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_EMPTY","",False,False,True,False),
            "PRESERVAR_CLASSIFICADO")
    def test_cache_corruption_blocked(self):
        self.assertEqual(audit.classify_readiness(
            "RAW_CORRUPT","",False,False,False,False),
            "BLOQUEIO_CACHE_INTEGRO")

if __name__=="__main__":
    unittest.main()
