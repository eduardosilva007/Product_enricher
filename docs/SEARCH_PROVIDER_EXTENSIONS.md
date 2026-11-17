# Coleta experimental: ScraperAPI e OpenSERP

## Escopo e segurança
- Os três provedores antigos (Tavily, Serper e Bright Data) permanecem inalterados.
- ScraperAPI é selecionável **manualmente**, mas **não** entra no roteador automático.
- OpenSERP agora é selecionável manualmente em `--search-provider openserp`, reutilizando artefato validado sem novas Search requests.
- Octoparse fora do escopo.
- Não há promoção automática ao cache principal, classificação ou alteração na taxonomia.

## ScraperAPI: uso manual controlado
Configurar `SCRAPERAPI_API_KEY` no `.env` já carregado pelo pipeline.
Usar `python pipeline_produtos.py --mode collect --search-provider scraperapi --max-search-new 1`
somente com massa de entrada controlada, depois de revisar plano da coleta.
A API recebe **uma** GET para a busca Google com autoparse, sem retry/fallback.
Links `google.com/goto` são descartados; a ausência de URLs diretas válidas
não dispara nova busca.

## OpenSERP: importação de artefatos
O piloto de 17/11/2026 teve 21 resultados brutos (7 para cada query).
Desses, 15 eram `google.com/goto` e ficam bloqueados.
Seis URLs diretas permanecem candidatas, **ainda sujeitas ao Source Sieve**.

1. Salvar o `pilot_summary.json` extraído do GitHub Actions.
2. Criar um manifesto CSV, com as chaves explícitas da base de entrada:
   ```csv
   query,brand,model
   STIHL MS382,STIHL,MS382
   STIHL FS250,STIHL,FS250
   NOVABOT N2000,NOVABOT,N2000
   ```
3. Executar:
   ```powershell
   & "C:\p\venv\Scripts\python.exe" .\import_openserp_staging.py --input .\pilot_summary.json --manifest .\openserp_manifest.csv --output .\openserp_staging.json
   ```
4. Conferir `source_audit` e `approved_candidates` do arquivo gerado.

IMPORTANTE: `openserp_staging.json` não é cache oficial. Não copiá-lo diretamente
para `cache_tavily.sqlite`. É necessário desenvolver e validar a promoção segura.

## Piloto OpenSERP de ponta a ponta, isolado

`run_openserp_full_pilot.py` executa o mesmo `--mode full`, utilizando 3 SKUs já
capturados (STIHL MS382, STIHL FS250 e NOVABOT N2000), o mesmo Source Sieve,
um cache SQLite novo e classificação JEV. Master/Final/DB/Input são criados
dentro de uma pasta de piloto; o código verifica os hashes dos originais.

Pré-verificação sem APIs:
```powershell
& "C:\p\venv\Scripts\python.exe" .\run_openserp_full_pilot.py --prepare-only
```

Piloto com limite de 15 chamadas JEV e 30.000 tokens:
```powershell
& "C:\p\venv\Scripts\python.exe" .\run_openserp_full_pilot.py --llm-provider jev --max-api-calls 15 --max-tokens 30000
```

Os relatórios aparecem em `piloto_argentina/openserp_full/<data>_<hash>/`,
incluindo `pilot_manifest.json`, `pipeline_full.log` e XLSX de resultados.
`network_requests=0` refere-se exclusivamente a **novas chamadas Search**;
fetch local de URL pós-sieve e JEV continuam possíveis.

### Limitação explícita

O piloto confirma o fluxo completo usando evidência capturada anteriormente.
**Ainda não captura novos SKUs diretamente via OpenSERP.** Para ampliar nesta noite
é necessário integrar a geração de artefatos via workflow de GitHub Actions,
com limites, estado assíncrono e importação atômica. Não liberar a ampliação
automática antes dessa etapa.


## Gates de QA
```powershell
& "C:\p\venv\Scripts\python.exe" -m py_compile .\pipeline_produtos.py .\search_provider_extensions.py .\import_openserp_staging.py
& "C:\p\venv\Scripts\python.exe" -m unittest discover -s tests -p test_search_provider_extensions.py -v
```
Sem autorização para habilitação automática ou campanha em massa.
