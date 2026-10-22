# PIPELINE v4.0.7.2-identity-runtime-guard

## Origem

Auditoria do piloto R9.6p7.2.1:
- 5/7 produtos migraram de NO_TARGET_EVIDENCE para READY_FOR_CLASSIFICATION;
- ANTHBOT e Briggs foram recuperados com múltiplas fontes coerentes;
- MILWAUKEE 2446 21XC M12 permaneceu NO_TARGET apesar de haver informação explícita na web;
- o relatório mostrou Search Model = "2446 21XC M12", indicando drift entre o comportamento esperado e o runtime local;
- uma fonte de ANTHBOT Genie 600 chegou a ACCEPT_TARGET_ISOLATED para o alvo Genie 3000 porque o target apareceu no BODY.

## Objetivos

1. Tornar resolução de configuração de marca robusta a &, espaços, pontuação e caracteres invisíveis.
2. Impedir consumo de Search quando regressões conhecidas de Search Model não estão ativas.
3. Bloquear páginas cujo TITLE aponta explicitamente para outro SKU quando o target aparece somente no BODY.
4. Preservar todos os gates de AUTO/Clef/taxonomia existentes.

## Alterações

### Brand config lookup por identidade compacta

As listas brand-specific deixam de depender exclusivamente da chave visual exata.

Exemplo:
- MILWAUKEE
- MILWAUKEE + caractere invisível

resolvem para a mesma configuração sem fuzzy matching.

### Identity Runtime Contract fail-closed

Cada execução valida casos de regressão antes de qualquer Search:
- ANTHBOT GENIE 1000 EU -> GENIE 1000
- MILWAUKEE 2446 21XC M12 -> 2446 21XC
- variante MILWAUKEE com caractere invisível -> 2446 21XC
- Briggs internal code -> Model-Type-Trim

Se qualquer caso divergir, a execução é bloqueada.

### TITLE other-SKU guard

Nova decisão:
- REJECT_TITLE_OTHER_SKU_BODY_ONLY

Regra:
- target não aparece no TITLE;
- TITLE contém outro SKU explícito;
- target aparece somente no BODY.

Nesse cenário a fonte não entra em curated evidence.

Isso bloqueia casos como:
- TITLE: ANTHBOT Genie 600 ...
- BODY contém tabela com Genie 3000

sem bloquear páginas oficiais genéricas da família cujo título não aponta para outro SKU.

## Versionamento

- Pipeline: 4.0.7.2-identity-runtime-guard
- Collection: R9.6p7.2.2
- Identity profile: 2026-10-22-safe-brand-key-v2
- Rules: permanecem 2026-10-13-v3.0.13-manual-taxonomy-gold-2

## Contratos preservados

- 1 Search request máximo por produto
- sem retry
- sem segunda busca
- sem provider failover
- sem Tavily Extract
- sem Advanced
- sem fuzzy identity
- sem alteração de Clef/BGE/reranker thresholds
- sem alteração de regras_negocio.json
- rescue-only continua proibido de AUTO
- Revisão B.I=OK continua HARD LOCK

## Validação

CI executa:
- py_compile
- testes v4.0.7
- testes v4.0.7.1
- testes v4.0.7.2
- validação sintática dos JSONs

## Próximo passo

Após merge:
1. git pull
2. python pipeline_produtos.py --mode validate
3. confirmar "IDENTITY RUNTIME CONTRACT OK"
4. recoletar somente MILWAUKEE 2446 21XC M12
5. verificar Search Model = 2446 21XC
6. se READY/PARTIAL e fontes limpas, liberar classificação do cohort atual
