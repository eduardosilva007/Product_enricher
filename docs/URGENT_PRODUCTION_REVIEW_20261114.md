# Revisão operacional urgente — 2026-11-14

Escopo: leitura estática da branch `fix/sqlite-canonical-sku-merge-20261113` e avaliação dos logs compartilhados; **não** houve acesso à máquina Windows, arquivos Excel oficiais, bancos SQLite reais nem credenciais.

## P0 — Final e Consolidado não são commit atômico
`pipeline_produtos.py:9430+`: `persist_baseline_excels` chama `_atomic_write_dataframe(master_df, ...)` e em seguida `_export_base_final(...)`. Cada arquivo é substituído individualmente; se o segundo falhar, o primeiro já foi salvo. A rotina só sincroniza SQLite depois dessas duas operações. O caller captura a exceção e informa `BASELINE EXCEL NÃO ATUALIZADA` (generalização incorreta no caso de commit parcial). **Não é seguro repetir a execução paga automaticamente.**

**Correção proposta, ainda NÃO implementada:** estagiar ambos os Excel, comprovar igualdade física e HARD LOCK, verificar hashes, preparar backups imutáveis, coordenar gravação e journal de recuperação, simular falha após o primeiro commit e testar rollback/reconciliação. Não mudar esse caminho enquanto uma execução real está ativa.

## P0 — JEV proposta incorretamente aceita no Excel público (reproduzido RED→GREEN)
`_record_is_accepted` rejeita corretamente `CLASSIFIED_CLEF` com `Review Required=SIM` (proposta não homologada). Porém, `_export_base_final` publica apenas 11 colunas, sem `Resolution State`; a função `_baseline_row_accepted` originalmente considerava `Revisão=PROVÁVEL` suficiente, mesmo com `Próxima Ação=DOUBLE_CHECK`.

**Falha reproduzida:** no commit `4e3a555`, 2 testes falharam na CI porque a mesma proposta era rejeitada no Consolidado e aceita pela Base Final. A validação pós-exportação compara números de aceites com a projeção da classificação e pode disparar `BASELINE_COHORT_MISMATCH` **depois de gravar** os arquivos. Não é prova de que os resultados foram perdidos.

**Correção isolada:** `af52dbc` impede que `PROVÁVEL` junto com `DOUBLE_CHECK`, `AGUARDAR_REVISAO_PRODUTO_CANONICO` ou `REVISAR_CONFLITO_EQUIVALENCIA` vire aceite automático; `Manual` e `BI_OK` continuam prioritários. Testes Windows e Linux passaram em GH Actions, mas falta revisão da baseline real. **Não usar a branch em uma execução real ativa.**

**Risco a verificar antes de escala:** propostas preservadas para B.I. não devem entrar repetidamente na fila de classificação como se nunca tivessem sido processadas. A política de `processed` precisa distinguir aceitação automática de classificação técnica concluída e aguardando revisão. Esse gate de fila ainda não foi comprovado com dados reais.

## P1 — Coortes e contagens não têm equivalência 1:1
`collect_products` escreve `Coleta_Search_R9_6p7_2_3.xlsx`, enquanto `build_classification_cohort` remove processados, locks, RAW inválido e evidência insuficiente. O `--max-search-new 850` é teto de requests, não quantidade de linhas classificáveis. A antiga seleção do modo `full` limitava antes do filtro de processados; corrigida em `93f55cb`, com CI verde, mas não reproduzida localmente.

**Fato observado:** 150 da primeira rodada e 497 da segunda não têm interseção ou persistência comprovada. Portanto, 203 é **diferença aritmética**, não pendências demonstradas.

## P1 — Histórico separado e reprocessamento
`pipeline_produtos.py:4842+`: `classification_results` conserva checkpoints por SKU/hash/modelo. `Classificacao_v3.xlsx` é substituída ao finalizar outra rodada. Históricos de modelos/regras podem coexistir; relatórios devem distinguir evidência atual, classificação histórica, proposta e homologação humana.

## P1 — Exportação pode perder multiplicidade
`_export_base_final` emprega `drop_duplicates("__key", keep="last")` antes de reconstruir pares exatos em uma etapa posterior. Reintroduzir pares é uma proteção parcial, mas não prova que cópias físicas duplicadas e todas as colunas permanecem idênticas. Antes de alterar: teste com pares idênticos repetidos, aliases estruturalmente iguais, `Revisão B.I=OK`, títulos e URLs divergentes e comparações físicas linha a linha.

## P1 — Escala sem fechamento financeiro
A configuração JEV permite teto `0=SEM_LIMITE`; `--max-items` limita produtos, não chamadas HTTP/tokens. A primeira linha de defesa para 18K é um orçamento explícito de Search, HTTP JEV e tokens por lote, mais parada em qualquer bloqueio de persistência. A Base de entrada precisa conter exatamente os produtos pretendidos.

## Ação deste pacote
`scripts/audit_full_reconciliation_readonly.py` faz conciliação por par exato Marca+Modelo entre relatório da coleta, cache, checkpoints, resultado Excel e Base Final, sem APIs e sem alterar bases oficiais. Executar **apenas depois de encerrar a rodada atual**:

```powershell
& "C:\p\venv\Scripts\python.exe" .\scripts\audit_full_reconciliation_readonly.py
```

O CSV traz cada SKU, motivo de exclusão ou conflito e evidência de gravação; o JSON agrega os totais. Verificar que `classification_result_xlsx_rows`, `latest_runs`, `status_counts` e `collection_report_requests` pertencem ao escopo pretendido. Quando a origem e a saída estiverem em coortes diferentes, nunca inferir pendentes por subtração.

## Release gate
**STATUS: BLOCKED_FOR_SCALE.** CI sintética e análise estática não substituem validação read-only dos Excel e bancos locais, teste real controlado, backup, postrun `tools/release_gate.py` e verificação da persistência por SKU. Não alterar o código em execução.
