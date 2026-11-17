# Coleta experimental: ScraperAPI e OpenSERP

## Escopo e segurança
- Os três provedores antigos (Tavily, Serper e Bright Data) permanecem inalterados.
- ScraperAPI é selecionável **manualmente**, mas **não** entra no roteador automático.
- OpenSERP continua fora de `--search-provider`; a importação de artefatos é offline e auditada.
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
para `cache_tavily.sqlite`. É necessário desenvolver/validar a promoção de registros
com integridade e histórico, sem sobrescrever classificações ou locks humanos.

## Gates de QA
```powershell
& "C:\p\venv\Scripts\python.exe" -m py_compile .\pipeline_produtos.py .\search_provider_extensions.py .\import_openserp_staging.py
& "C:\p\venv\Scripts\python.exe" -m unittest discover -s tests -p test_search_provider_extensions.py -v
```
Sem autorização para habilitação automática ou campanha em massa.
