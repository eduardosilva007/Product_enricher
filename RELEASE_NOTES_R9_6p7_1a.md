# R9.6p7.1a — Evidence Retention + Safe Semantic Hints

Base: R9.6p7 estável. Sem merge com R9.7/p8.

## Coleta
- Mantém hard rule: 1 produto = no máximo 1 Search request; sem retry/fallback/provider Extract.
- Body Identity Rescue permanece conservador: BODY só pode resgatar identidade com Marca+Modelo próximos; no máximo PROBABLE.
- Tavily: persiste apenas janela target-anchored (até 5 KB), nunca o raw_content integral.
- Serper/Bright Data: quando houver enriquecimento/fetch local, a mesma janela target-anchored é persistida em `results[*]._target_evidence_window`.
- A janela é preservada mesmo se Target Isolation rejeitar a fonte depois do fetch, permitindo reavaliação futura sem nova Search.
- Metadados: `_target_evidence_source`, `_target_evidence_meta`, `_raw_persistence_state`.

## Classificador
- Stage 1/AUTO permanece baseado apenas em evidência target-anchored e gate STRONG.
- Modelo e títulos RAW passam a produzir uma trilha separada de `Review Type Hints`.
- Hints NÃO entram em `Stage1 Type Candidates`, NÃO resolvem TYPE, NÃO entram no funil LLM final sozinhos.
- Hard rejects (`WRONG_MODEL`, `COMPATIBILITY_ONLY`, `RELATED_ONLY`, `QUERY_IDENTITY_DRIFT`) não alimentam hints de título.
- Plural simples é tolerado somente na trilha de review (ex.: `centrifugal pumps` -> alias `centrifugal pump`).
- Novas colunas: `Review Type Hints`, `Review Suggested Type`, `Review Hint Evidence`.
- Nova ação operacional: `TYPE_HINT_REVIEW` / `TYPE_HINT_REVIEW_FAMILY_JA_DEFINIDA`.

## Regressões verificadas
- `test_import_noise_taxonomy_r96p7.py`: OK.
- `test_r96p71a_safe_hints.py`: OK.
- `python pipeline_produtos.py --mode validate`: QUALITY GATES OK (80 synthetic, 17 identity, 22/34 real_explicit).
- Cross-brand regression: HYUNDAI `42004 000104` com título TCL não resolve automaticamente e não passa o funil final.

## Smoke operacional recomendado
Com `Base de Produtos.xlsx` contendo os itens novos:
1. `python pipeline_produtos.py --mode validate`
2. Menu `3 - PROCESSO COMPLETO`
3. Provider: o desejado (ENTER = Tavily)
4. Limite coleta: `20`
5. Limite classificação: ENTER
6. Recoletar RAW fraco: `N`
7. Conferir pré-coleta e só então digitar `CONFIRMAR`.

## Git
Sugestão após validar o smoke:

```bash
git add pipeline_produtos.py pipeline_config.json regras_negocio.json collection_quality_policy.json test_r96p71a_safe_hints.py
git commit -m "R9.6p7.1a retain evidence and add safe review hints"
git tag -a r9.6p7.1a -m "Stable classifier: evidence retention + safe semantic hints"
```
