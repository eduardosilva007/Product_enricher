# Argentina — preparação da coleta noturna (09/11/2026)

## Escopo e estado real
Branch de preparação, **não pronta para executar 14 mil consultas**. Nenhum arquivo produtivo foi alterado. A integração do gestor multiprovedor à função `collect_products`, o controle durável SQLite e a verificação do saldo real dos fornecedores ainda precisam ser implementados/validados.

### O que esta branch contém
- `tests/test_argentina_readiness.py`: regressão estrutural SKU, cache RAW_EMPTY e política atual de requests.
- `provider_quota_planner.py`: planejador **offline**, para múltiplos provedores/credenciais e bloqueio de um mesmo SKU após tentativa; não envia HTTP, não persiste no SQLite.
- `tests/test_provider_quota_planner.py`: alternância, chave compartilhada, falha incerta, cota local e credencial indisponível.

## Executar ao chegar em casa
No PowerShell, dentro de `C:\p\Product_enricher`:

```powershell
git status --short
git fetch origin
git diff --stat origin/main
# Inspecionar antes de trocar de branch: seu arquivo local/menu difere do GitHub.
git switch feature/argentina-nightly-readiness-20261109
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_argentina_readiness.py" -v
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_provider_quota_planner.py" -v
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode validate
```

Se houver alterações locais, salvá-las antes do `git switch`; não usar `git reset --hard`. Alguns testes da versão original podem falhar: são diagnósticos, não licença para coletar. Em ambiente local, confirmar `git status` e as diferenças de versão, configuração e políticas.

## Contratos da futura integração multiprovider
1. Cada produto tem no máximo **uma request iniciada** na rodada. Falha, timeout e resposta ambígua nunca provocam segunda request automática.
2. Troca automática de **credencial/provedor somente para o próximo produto não consultado**.
3. Cota configurada por chave/conta, com agrupamento de orçamento compartilhado; uma segunda API key **não** significa saldo adicional.
4. Limites explícitos de orçamento, snapshots de consumo e journal de reservas **durável**, com reconciliação após crash. O planejador offline ainda NÃO satisfaz essa durabilidade.
5. Rate-limit transitório não equivale a cota esgotada: gerenciar cooldown sem burlar limites.
6. Se todas as cotas atingirem limite, interromper de forma limpa; preservar checkpoints; nenhuma chamada extra.
7. `RAW_EMPTY` jamais conta como coleta útil. Confrontar histórico de tentativas/consumo antes de recoletar.
8. O RAW válido, título validado e classificação são estados independentes; impedir sobrescrita de revisão humana.
9. Identidade: gate marca+modelo exato/core antes do semântico; n-gram char 2–5 somente para ranking *após* elegibilidade; hard negatives com 2446-21XC/2446-20, CTVLI20018/CGTLI20018, GSR/GSB.
10. Segurança: nunca registrar valores de API keys no log nem comitar `.env`.

## Gatilhos para a coleta noturna
- Comparar a versão local com main; resolver discrepância do menu.
- Inventariar 1.030 RAW_EMPTY, histórico de tentativas e contratos do cache.
- Integrar o roteador offline ao executor real com SQLite transacional e testes mockados de HTTP 429/403/timeout.
- Executar um piloto real de 10–20 produtos com limite de créditos explícito e fontes verificadas.
- Medir taxa de títulos e identificação correta em casos Gold; zero regressão nas travas estruturais.
- Só então executar lote maior, com limite diário de requests e relatório de classificação por Business Key.

## FUP de 16/11
Confrontar a Base Final argentina com o universo aproximado de 30 mil: quantos possuem Category+FAMILY válidos; quantos ainda pendem; quantos estão sob lock manual; quantos títulos comprovados, separadamente. Não confundir processados, cacheados, ou linhas Excel com classificados.

**Não executar `--mode full` nem `--recollect-empty` irrestritos até cumprir esses gates.**
