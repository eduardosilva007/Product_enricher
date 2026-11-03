#!/usr/bin/env python3
"""Regression tests for read-only status classification. No HTTP, no Excel writes."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_status_evidence_readonly import evaluate

def row(**kw):
    result={"Título Real":"","O que é (Tipo)":"","Alimentação":"",
        "Category General":"","FAMILY":"","Revisão B.I":"","Status":"Script"}
    result.update(kw)
    return result

class StatusTests(unittest.TestCase):
    def test_no_cache_is_not_claimed_never_collected(self):
        s=evaluate(row(),{"raw_state":"RAW_MISSING"})
        self.assertEqual(s["Status Coleta"],"NAO_COMPROVADA")
        self.assertEqual(s["Próxima Ação Recomendada"],"VERIFICAR_COLETA_ANTES_DE_BUSCAR")
    def test_title_without_cache_is_not_recollected_blindly(self):
        s=evaluate(row(**{"Título Real":"Bosch GOP 185-LI multi cutter"}),{"raw_state":"RAW_MISSING"})
        self.assertEqual(s["Status Coleta"],"EVIDENCIA_EXCEL_SEM_CACHE")
        self.assertEqual(s["Status Classificação"],"PENDENTE_CLASSIFICACAO")
        self.assertEqual(s["Próxima Ação Recomendada"],"AVALIAR_IDENTIDADE_E_CLASSIFICAR")
    def test_partial_with_title(self):
        s=evaluate(row(**{"Título Real":"KLD electric chainsaw KLDES16",
                "O que é (Tipo)":"CHAINSAW","Alimentação":"CORDED"}),{"raw_state":"RAW_OK"})
        self.assertEqual(s["Status Classificação"],"CLASSIFICACAO_PARCIAL")
        self.assertEqual(s["Próxima Ação Recomendada"],"RECUPERAR_CLASSIFICACAO_COM_EVIDENCIA")
    def test_wrong_title_cannot_confirm_identity(self):
        s=evaluate(row(**{"Título Real":"American Airlines AA7000"}),{"raw_state":"RAW_OK"})
        self.assertEqual(s["Status Identidade"],"IDENTIDADE_NAO_VERIFICADA")
    def test_complete_not_automatically_approved(self):
        s=evaluate(row(**{"Título Real":"Engine","O que é (Tipo)":"ENGINE",
                "Category General":"OTHERS","FAMILY":"ENGINE"}),{"raw_state":"RAW_OK"})
        self.assertEqual(s["Status Classificação"],"CLASSIFICACAO_COMPLETA_NAO_VALIDADA")
        self.assertEqual(s["Próxima Ação Recomendada"],"VALIDAR_CLASSIFICACAO")
    def test_gold_never_suggests_change(self):
        s=evaluate(row(**{"Revisão B.I":"OK","Título Real":"foo"}),{"raw_state":"RAW_EMPTY"})
        self.assertEqual(s["Próxima Ação Recomendada"],"PROTEGIDO_GOLD_NAO_ALTERAR")
    def test_cache_empty_not_missing(self):
        self.assertEqual(evaluate(row(),{"raw_state":"RAW_EMPTY"})["Status Coleta"],"COLETA_VAZIA")
    def test_identity_not_asserted_without_proof(self):
        s=evaluate(row(**{"Título Real":"Makita ABC123"}),{"raw_state":"RAW_OK"})
        self.assertNotEqual(s["Status Identidade"],"MARCA_MODELO_CONFIRMADOS")

if __name__=="__main__":
    unittest.main()
