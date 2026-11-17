# Novos provedores — coleta no pipeline oficial

## Fonte única e controles

O caminho oficial do cache é `cache_tavily.sqlite` (tabela existente `tavily_cache`).
O controle operacional permanece em `pipeline_v3.sqlite`, e Master, Base Final,
entrada e saída permanecem nos arquivos de produção definidos em
`pipeline_config.json`. A integração NÃO cria outro cache SQLite.
Os novos provedores rejeitam `--cache`, `--db` e outros overrides fora desses
caminhos. Não há alteração de schema do SQLite.

`--mode full` para OpenSERP ou ScraperAPI tem dois limites explícitos:
1–10 SKUs em coleta e 1–10 SKUs em classificação, além dos tetos LLM.
A classificação é restrita às chaves realmente coletadas e com evidência curada
na execução atual; não seleciona aleatoriamente registros antigos do cache.

Os provedores experimentais **não entram ainda no roteador automático noturno**.
O OpenSERP usa resultados de uma busca previamente executada, informados em
`openserp_staging.json`; não busca SKUs novos de forma automática.
A seleção de SKUs passa por filtro de input, hard locks, cache e reserva de
tentativa em `pipeline_v3.sqlite`.

## Operação: OpenSERP

A coleta em lote OpenSERP (1–10 pesquisas) já existe nos workflows
`.github/workflows/openserp-batch-lab.yml` e
`.github/scripts/openserp_batch_lab.py`. Ela publica JSON, sem tocar no
banco central. Para importar o artifact do piloto histórico:
```powershell
& "C:\p\venv\Scripts\python.exe" .\import_openserp_staging.py `
  --input ".\pilot_summary.json" `
  --manifest ".\samples\openserp_manifest_pilot.csv" `
  --output ".\openserp_staging.json"
```
O staging **não grava SQLite**; apenas a coleta principal o faz, após
passar pelos filtros. O importador não converte automaticamente o
`normalized_batch.json` do workflow batch; isso permanece pendente.

Executar o piloto completo no pipeline da produção, depois de conferir
que o `openserp_staging.json` corresponde aos SKUs ainda elegíveis na
`Base de Produtos.xlsx`:
```powershell
& "C:\p\venv\Scripts\python.exe" .\pipeline_produtos.py `
  --mode full `
  --search-provider openserp `
  --openserp-artifact ".\openserp_staging.json" `
  --llm-provider jev `
  --max-search-new 3 `
  --max-items 3 `
  --max-api-calls 15 `
  --max-tokens 30000
```
O comando coleta **somente SKUs elegíveis** (não classificados ou bloqueados
pela autoridade humana), diretamente no cache central. Preflight do staging
ocorre antes de reservar uma tentativa. Se não houver nenhum SKU com evidência
curada nesta rodada, o modo completo termina com código de erro em vez de
classificar aleatoriamente outros produtos.
`requests=1` para OpenSERP contabiliza *um artefato consumido por SKU*,
com `network_requests=0` para Search neste momento; enriquecimento local
de URLs e chamadas LLM continuam possíveis.

## Operação: ScraperAPI

Configure `SCRAPERAPI_API_KEY` no `.env` existente. A integração aceita
até um request Google por SKU, com limite obrigatório, sem retries/fallback.
Valide primeiro **1 SKU** com `--mode collect` e
`--search-provider scraperapi --max-search-new 1`. Após conferir
resultado e cache central, valide o modo full com caps explícitos.

## Gatilhos para autorizar a noite

Os testes de CI verificam sintaxe, SQLite round-trip, limites e contratos.
**Não equivalem a testes reais de pesquisa e classificação.**
Antes da execução noturna: auditoria das chaves, fontes, taxa de curadoria,
Category General e FAMILY, integridade do cache central e ausência de alterações
em Revisão B.I=OK. Sem aprovação humana, não habilitar auto-router experimental.

## Comandos de QA offline

```powershell
& "C:\p\venv\Scripts\python.exe" -m py_compile .\pipeline_produtos.py .\search_provider_extensions.py .\import_openserp_staging.py
& "C:\p\venv\Scripts\python.exe" -m unittest discover -s tests -p test_search_provider_extensions.py -v
```
