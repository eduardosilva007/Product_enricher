from pathlib import Path
import tempfile
import pandas as pd
import importlib.util
import sys

MODULE_PATH = Path('/mnt/data/pipeline_produtos.py')
spec = importlib.util.spec_from_file_location('pipeline_produtos', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

# Regression 1: legacy/master dataframe WITHOUT the two HF3 diagnostic columns.
# This exactly reproduces the production KeyError reported by the user.
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / 'Base Final Produtos Enriquecida.xlsx'
    legacy_df = pd.DataFrame([
        {
            'Marca': 'PEKTRA',
            'Modelo': 'PKCD1210CK',
            'O que é (Tipo)': 'DRILL',
            'Alimentação': 'BATTERY',
            'Category General': 'OTHERS',
            'FAMILY': 'POWER TOOLS',
            'Título Real': 'Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v',
            'Status': 'Script',
            'Revisão': 'AUTO_APROVADO',
            'Revisão B.I': pd.NA,
            'Resolution State': 'RESOLVED',
            'Review Required': 'NÃO',
        }
    ])
    n = mod._export_base_final(legacy_df, out)
    assert n == 1
    got = pd.read_excel(out, dtype=object)
    assert list(got.columns) == mod.BASELINE_FINAL_COLUMNS
    assert 'Título Referência' in got.columns
    assert 'Próxima Ação' in got.columns
    assert pd.isna(got.loc[0, 'Título Referência'])
    assert pd.isna(got.loc[0, 'Próxima Ação'])

# Regression 2: current dataframe WITH the HF3 columns must preserve values.
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / 'Base Final Produtos Enriquecida.xlsx'
    current_df = pd.DataFrame([
        {
            'Marca': 'PEKTRA',
            'Modelo': 'PKCD1210CK',
            'O que é (Tipo)': 'DRILL',
            'Alimentação': 'BATTERY',
            'Category General': 'OTHERS',
            'FAMILY': 'POWER TOOLS',
            'Título Real': 'Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v',
            'Título Referência': 'Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v',
            'Próxima Ação': 'CONCLUIDO',
            'Status': 'Script',
            'Revisão': 'AUTO_APROVADO',
            'Revisão B.I': pd.NA,
            'Resolution State': 'RESOLVED',
            'Review Required': 'NÃO',
        }
    ])
    n = mod._export_base_final(current_df, out)
    assert n == 1
    got = pd.read_excel(out, dtype=object)
    assert got.loc[0, 'Título Referência'] == 'Pektra PKCD1210CK Taladro Atornillador Inalambrico 12v'
    assert got.loc[0, 'Próxima Ação'] == 'CONCLUIDO'

print('HF3.1p2 BASELINE EXPORT TEST: OK')
