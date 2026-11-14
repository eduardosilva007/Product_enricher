# Release gate do piloto real de 50 SKUs — 2026-11-14

**Estado inicial:** NÃO LIBERADO para 19k sem piloto real, fonte confiável e QA humana.
Os testes GitHub Actions simulam Excel/SQLite; não exercitam as credenciais privadas nem a pesquisa JEV real.

## 1. Falha anterior: 338 foram classificados mas não chegaram à Base Final

Em 2026-11-14, `PROGRESSO 338/338` terminou, mas a exportação falhou:
`BASELINE_FILE_LOCKED: Base Final Produtos Enriquecida.xlsx`. O checkpoint e
`Classificacao_v3.xlsx` foram preservados. Não repetir APIs para forçar gravação.

Feche todos os Excel e processos da rodada. Na branch do piloto:
```powershell
cd C:\p\Product_enricher
& "C:\p\venv\Scripts\python.exe" .\scripts\replay_locked_classification_offline.py --expected-rows 338
```
Esse comando gera um staged preview e não modifica os oficiais. Ele exige o
`Classificacao_v3.xlsx` exato da rodada 338, mesmo hash de regras e
`Baseline Warning` contendo `BASELINE_FILE_LOCKED`. Bloqueia se outro
resultado já sobrescreveu a saída, se houver desvio de locks, se o número de
aceitos diferir ou se algum SKU classificado não aparecer na Final.

Somente após `STAGED_PASS_NO_WRITES`, feche Excel e:
```powershell
& "C:\p\venv\Scripts\python.exe" .\scripts\replay_locked_classification_offline.py --expected-rows 338 --commit
```
Refaz SOMENTE gravação e sincroniza índice SQLite; **0 Search, 0 JEV**.
Cria backup imutável de Consolidado/Final/SQLite antes do commit. Se falhar,
não rodar mais APIs; preservar log e backups.

## 2. Preparar piloto inédito de 50, sem APIs

Use Excel real de ~19 mil (colunas Marca+Modelo/Fabricante+Modelo); não use
`Base de Produtos.xlsx` de 1.030 produtos. Nenhum nome de arquivo foi
pressuposto. A seleção é por marca diversificada, determinística, somente
business keys ausentes do cache e do ledger de tentativas, sem hard locks.

```powershell
$source19k = Read-Host 'Caminho completo do Excel com os 19 mil SKUs'
& "C:\p\venv\Scripts\python.exe" .\scripts\pilot_50_full_gate.py prepare --source "$source19k"
```
Exige >=15 mil registros válidos, 50 chaves verdadeiramente novas; falha se
não houver. Produz `pilotos/stable_50_*/manifest.json`, o `pilot_50_input.xlsx`
de 50 e os hashes de origem, pipeline, regras, política, baseline, cache e SQLite.

## 3. Rodar exatamente 50 (único passo com APIs)

Antes, fechar os Excel, conferir 50 inéditos e budgets: 50 pesquisas Search,
no máximo 65 HTTP JEV e 350000 tokens JEV na execução. Custos podem ser
inferiores (regras) e não é garantia de custo monetário; depende do provider.

```powershell
& "C:\p\venv\Scripts\python.exe" .\scripts\pilot_50_full_gate.py execute --manifest "CAMINHO_DO_MANIFEST_GERADO" --confirm EXECUTAR_50
```

O executor:
1. verifica hashes congelados e ausência de tentativas anteriores;
2. verifica que os dois Excel não estão abertos e faz backup de ambos,
   2 SQLite, resultados anteriores e relatórios;
3. roda `pipeline_produtos.py --mode full` SOMENTE com o Excel de 50;
4. preserva todos os logs; aborta se a execução retornar erro;
5. verifica PROCV físico 50/50, resultados versus Base Final,
   aceitações, B.I=OK, título coletado visível e dados do cache;
6. grava `pilot_50_result.json` e `pilot_50_quality_review.xlsx`,
   contendo `Amostra_10_Validar` e `Todos_50`.

**Gate quantitativo de go/no-go:** >=35/50 com TYPE+Category+Family completos,
0 títulos de Search encontrados mas faltantes na Base Final, 0 perda de
aceitos e 0 violação humana. `CLASSIFIED_CLEF` é proposta, não aprovação.
Apenas este gate dá `TECHNICAL_PASS_AWAITING_QUALITY_REVIEW`.

**Gate qualitativo:** validar manualmente os 10 da aba
`Amostra_10_Validar` comparando marca, modelo, título real, tipo, fonte,
Category e Family. Marcar `OK` na última coluna apenas em classificações
corretas; `ERRO` quando incorretas. Um só erro na amostra bloqueia a massa,
não se deve alterar para OK apenas para liberar script.

## 4. Autorizar 19k somente após os dois gates

```powershell
& "C:\p\venv\Scripts\python.exe" .\tools\verify_pilot_50_release.py --report "CAMINHO_PILOT_50_RESULT_JSON" --source "$source19k"
```

Precisa retornar `APPROVED_FOR_BOUNDED_MASS_RUN` após 10 OK. O executor
`scripts/run_bulk_full_night.ps1 -InputFile "$source19k" -PilotReport
"CAMINHO_PILOT_50_RESULT_JSON" -ExecuteLive` **revalida** o gate.

O script em massa usa backups, lotes e limites, parando no primeiro erro.
Ele não promete 19 mil concluídos nem aprovação de 100% dos itens.

## Hard blockers
- Excel aberto ou falha de persistência;
- mismatch entre `Classificacao_v3.xlsx` e SKU da Final;
- falso `AUTO_APROVADO` de proposta JEV;
- título Search disponível mas invisível no Excel público;
- perda ou alteração de linhas humanas;
- SQL corrompido/chaves ambíguas/reconsumo de consulta;
- menos de 35/50 classificações completas ou erro de qualidade na amostra.

Nenhum resultado pode ser "forçado" a aprovado para satisfazer a meta.
