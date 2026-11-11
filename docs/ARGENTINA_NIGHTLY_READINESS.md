# Argentina — preparação da coleta noturna (09/11/2026)

## Escopo e estado real
Branch de preparação para campanhas assistidas, **não liberada para consultas em massa sem limite**. O roteador multiprovedor **já está integrado** a `collect_products()` via `DurableRouter`, `configured_slots()` e reservas SQLite antes da chamada HTTP. Dois pilotos isolados de 20 SKUs concluíram coleta e classificação com auditorias de integridade PASS; os resultados **não foram promovidos ao cache produtivo**. A integração por execução não comprova reconciliação de cotas **entre múltiplos diretórios isolados** nem saldo real de fornecedor. A validação taxonômica humana continua pendente.

### O que esta branch contém
- `tests/test_argentina_readiness.py`: regressão estrutural SKU, cache RAW_EMPTY e política atual de requests.
- `provider_quota_planner.py`: planejador **offline**, para múltiplos provedores/credenciais e bloqueio de um mesmo SKU após tentativa; não envia HTTP, não persiste no SQLite.
- `tests/test_provider_quota_planner.py`: alternância, chave compartilhada, falha incerta, cota local e credencial indisponível.
- `nightly_provider_router.py`: roteador ativo com reserva SQLite antes de HTTP; bloqueio de retry por SKU e de limites divergentes no mesmo `billing_group`.
- `run_controlled_night_pilot.py`: executor isolado, limitado a 20 SKUs por execução, com coleta → classificação → auditoria e sem promoção automática.
- `.github/workflows/validate.yml`: executa os testes de raiz e descoberta da pasta `tests/` com dependências de CI.

## Executar ao chegar em casa
No PowerShell, dentro de `C:\p\Product_enricher`:

```powershell
git status --short
git branch --show-current
git rev-parse HEAD
# Nao executar git switch, pull, reset ou checkout de arquivos sem comparar a arvore local.
# A instalacao local contem arquivos modificados e nao rastreados importantes.
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_argentina_readiness.py" -v
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_provider_quota_planner.py" -v
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode validate
```

Se houver alterações locais, salvá-las antes do `git switch`; não usar `git reset --hard`. Alguns testes da versão original podem falhar: são diagnósticos, não licença para coletar. Em ambiente local, confirmar `git status` e as diferenças de versão, configuração e políticas.

## Contratos vigentes e pendências da integração multiprovider
1. Cada produto tem no máximo **uma request iniciada** na rodada. Falha, timeout e resposta ambígua nunca provocam segunda request automática.
2. Troca automática de **credencial/provedor somente para o próximo produto não consultado**.
3. Cota configurada por chave/conta, com agrupamento de orçamento compartilhado; uma segunda API key **não** significa saldo adicional.
4. Limites explícitos de orçamento, snapshots de consumo e journal de reservas **durável**, com reconciliação após crash. O planejador offline não satisfaz essa durabilidade; o DurableRouter persiste por SQLite isolado, mas a reconciliação cumulativa entre pilotos continua pendente.
5. Rate-limit transitório não equivale a cota esgotada: gerenciar cooldown sem burlar limites.
6. Se todas as cotas atingirem limite, interromper de forma limpa; preservar checkpoints; nenhuma chamada extra.
7. `RAW_EMPTY` jamais conta como coleta útil. Confrontar histórico de tentativas/consumo antes de recoletar.
8. O RAW válido, título validado e classificação são estados independentes; impedir sobrescrita de revisão humana.
9. Identidade: gate marca+modelo exato/core antes do semântico; n-gram char 2–5 somente para ranking *após* elegibilidade; hard negatives com 2446-21XC/2446-20, CTVLI20018/CGTLI20018, GSR/GSB.
10. Segurança: nunca registrar valores de API keys no log nem comitar `.env`.

## Gatilhos para a coleta noturna
- Comparar a versão local com main; resolver discrepância do menu.
- Inventariar 1.030 RAW_EMPTY, histórico de tentativas e contratos do cache.
- **Já executado:** integrar DurableRouter no coletor com SQLite transacional; testes de 429, ausência de orçamento e retomada por reserva existem.
- **Pendente crítico para escalar:** garantir reserva e consumo por conta/grupo globalmente entre pilotos isolados, sem reiniciar cotas ao copiar o banco original; validar limites cumulativos de Search e JEV.
- Executar um piloto real de 10–20 produtos com limite de créditos explícito e fontes verificadas.
- Medir taxa de títulos e identificação correta em casos Gold; zero regressão nas travas estruturais.
- Só então executar lote maior, com limite diário de requests e relatório de classificação por Business Key.

## FUP de 16/11
Confrontar a Base Final argentina com o universo aproximado de 30 mil: quantos possuem Category+FAMILY válidos; quantos ainda pendem; quantos estão sob lock manual; quantos títulos comprovados, separadamente. Não confundir processados, cacheados, ou linhas Excel com classificados.

**Não executar `--mode full` nem `--recollect-empty` irrestritos até cumprir esses gates.**
