# Auditoria de Taxonomia — Manual vs Congelada

## Escopo

Comparação de 65 itens presentes nas duas bases.

Por orientação do negócio, `O que é (Tipo)` e `Alimentação` NÃO foram avaliados nem alterados.
A referência (gold) desta revisão é exclusivamente:

- `Category General`
- `FAMILY`

## Resultado antes do ajuste

- 65 itens comparáveis
- 2 pares `Category General + FAMILY` já coincidiam com a base manual
- 63 pares divergiam

Esse número não representa acurácia geral do pipeline: o arquivo é um conjunto de itens manualmente corrigidos e, portanto, é naturalmente concentrado em divergências de taxonomia.

## Estratégia aplicada

A revisão foi propositalmente conservadora para preservar o comportamento do piloto Clef:

1. Nenhuma alteração em `pipeline_produtos.py`.
2. Nenhuma alteração em `pipeline_config.json`.
3. Nenhuma alteração nos aliases de `PRODUCT_TYPES`.
4. Nenhuma alteração nos fallbacks globais de PART, GENERATOR, ENGINE, DUSTER etc.
5. Inclusão somente dos pares de taxonomia manualmente validados que faltavam no schema.
6. Inclusão de overrides de taxonomia por `Marca + Modelo` para os itens manualmente validados.
7. Uma única correção genérica de precedência para WATERPUMP:
   - `CENTRIFUGAL PUMP` explícito passa a ter prioridade 304;
   - `PRIMING PUMP` continua prioridade 300;
   - portanto `centrifugal pump` vence quando coexistir com `self-priming`;
   - classes mais específicas como SUBMERSIBLE/CIRCULATION/PERIPHERAL continuam acima quando aplicáveis.

## Alterações quantitativas

- TAXONOMY_PAIRS: 185 -> 200
- Novos pares válidos: 15
- TAXONOMY_EVIDENCE_RULES: 35 -> 93
- Overrides manuais específicos adicionados: 58
- PRODUCT_TYPES: permanece 151
- Python: sem alteração

## Cobertura do gold

Após o ajuste:

- 60/60 itens cujo par manual é representável pelo schema atual mapeiam exatamente para o gold.
- 5 itens manuais usam `OTHERS | OTHERS` e NÃO foram forçados no JSON.

Os 5 casos são:

- PECA | PE 0158
- AKWEL SA | 643475
- KASHON | HYDRAULIC TORQUE
- FORMULA 650 | FORMULA 650
- IRINOX | 270223129

Motivo: o executor atual proíbe explicitamente `OTHERS | OTHERS` como dead-end de taxonomia. Alterar isso exigiria mudança no Python, o que foi evitado nesta revisão para proteger o fluxo Clef já validado.

Esses registros já validados com `Revisão B.I=OK` continuam protegidos pelo hard lock da base.

## Versão

Rules Version:
`2026-10-13-v3.0.12-manual-taxonomy-gold`

Semantic hash:
`4714d126be18b877d9c1c04b17b38035775c45da22e897a74b35869f545321e7`

## Validação executada

A classe `Rules` da v4.0.4 foi carregada com o novo JSON:

- 151 PRODUCT_TYPES
- 200 TAXONOMY_PAIRS
- 93 TAXONOMY_EVIDENCE_RULES
- 0 erros de schema/validação

Também foi executado teste de mapeamento contra os 65 itens manuais:

- 60 representáveis: 60/60 exatos
- 5 `OTHERS | OTHERS`: deliberadamente não forçados

## Próximo stress recomendado

Usar itens ainda não presentes neste gold para evitar medir memorização dos overrides.

Sugestão:
1. validar o JSON com `python pipeline_produtos.py --mode validate`;
2. rodar 100 itens novos via opção 4;
3. fazer double-check dos 100;
4. medir exact match de `Category General + FAMILY`;
5. manter separadas as métricas RULE, CLEF_CLASSIFIED, CLEF_REPAIR e NEEDS_RECOLLECTION.

O objetivo desta revisão é enriquecer a taxonomia disponível ao funil/Clef sem alterar a arquitetura que apresentou bom resultado no piloto.
