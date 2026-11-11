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

## Master overnight - estabilizacao 11/11/2026

A branch `feature/overnight-master-stabilization-20261111` centraliza o motor
operacional no **`pipeline_produtos.py`**. Os arquivos
`run_controlled_night_pilot.py` e `nightly_provider_router.py` permanecem
apenas como importadores de compatibilidade, sem regras operacionais duplicadas.

O modo `--mode overnight` executa **coleta -> classificacao -> auditoria**
sobre lotes isolados de ate 20 SKUs, encadeando ate o total solicitado
(`--campaign-total 50/100/250/500/...`). O bloqueio de repeticao usa os
cohorts anteriores e o historico de reservas SQLite; contas bloqueadas e
limites de grupos compartilhados tambem persistem entre lotes. Os totais de
JEV e tokens sao **reservados conservadoramente por sublote**, de modo que
um reinicio nao gera uma segunda cota para o mesmo sublote.

Exemplo de **amostra de 50 novos SKUs**, apenas apos confirmar chaves,
cotacoes, orcamento e backups (consome APIs reais):

```powershell
& C:\p\venv\Scripts\python.exe C:\p\Product_enricher\pipeline_produtos.py --mode overnight --execute-live --allow-prior-serper-quota-error --campaign-total 50 --search-budget 20 --llm-budget 60 --token-budget 120000 --campaign-jev-budget 180 --campaign-token-budget 360000 --timeout-minutes 120
```

Para auditoria offline sem chamadas HTTP: executar o mesmo master com
`--mode overnight` **sem** `--execute-live`.

Para retomar apos interrupcao, usar o mesmo comando original adicionando
`--campaign-resume-dir <CAMPAIGN_DIR>`, preservando todos os orcamentos e
a autorizacao original. O checkpoint fica em
`piloto_argentina/controlled_night/campaigns/<ID>/campaign.json`.

**Gates e limites:** nao e uma autorizacao para rodada irrestrita;
nenhuma promocao automatica ao cache produtivo ou aprovacao de taxonomia
humana e permitida. Qualquer falha de coleta, classificacao, auditoria,
reservas, cobertura RAW_OK ou limite JEV/token interrompe o encadeamento.
Os resultados permanecem segregados por sublote ate revisao e reconciliacao
auditadas. Nao confundir 50 SKUs processados com 50 taxonomias corretas.

## Teste de interrupção e retomada controlada (5+5)

Para fechar a amostra de 100 SKUs, a campanha de 10 deve comprovar
retomada sem repetição de SKUs nem exceder os limites reservados.

1. Rodar `--campaign-total 10 --search-budget 5`, com orçamento global
   explícito, acrescentando `--campaign-pause-after-sublots 1`.
   Após o primeiro sublote auditado, o master deixa o manifesto
   `status=PAUSED_FOR_RESUME_TEST` e `audited_products=5`, com
   `production_persisted=false`. Essa pausa não é crash no meio da API.
2. Copiar o caminho exato de `campaign` exibido no relatório (o arquivo
   `campaign.json`). Retomar com o mesmo comando e os mesmos valores de
   target/budgets/Serper, retirando a pausa e acrescentando
   `--campaign-resume-dir "<diretório pai de campaign.json>"`.
3. Aceitar somente `status=COMPLETE`,
   `production_persisted=true`, `audited_products=10`,
   dois sublotes auditados com chaves únicas e reservas totais
   sem ultrapassar os budgets. A promoção canônica ocorre somente após
   o segundo sublote ser auditado.

**Limitação intencional:** interromper um worker JEV já iniciado *não*
tem retomada automática; o processo retorna
`JEV_INCOMPLETE_REQUIRES_AUDIT` para não repetir chamadas/tokens que
podem ter sido cobrados. Não considerar esse bloqueio uma retomada bem-sucedida.
Somente uma reconciliação explícita da contabilidade JEV poderia tornar
segura a retomada de um worker interrompido. A pausa 5+5 testa
retomada entre checkpoints completos, não exatamente-once em uma
requisição HTTP em andamento.

## Persistência canônica de resultados overnight (11/11/2026)

**A Base Final e o Consolidado são as fontes oficiais.** O cache SQLite
canônico guarda a evidência RAW e os estados de classificação ficam nos
Excels oficiais, incluindo propostas ainda pendentes de double check.

O master agora grava o cache canônico, o banco SQLite oficial de checkpoints/LLM, o Consolidado e a Base Final
**somente após auditoria de campanha COMPLETE**. A gravação reutiliza
`persist_baseline_excels` e suas travas Manual / Revisão B.I=OK.
Registros que precisam de revisão são persistidos como pendentes,
**sem promoção para aprovação humana**.

Para incorporar a campanha de 50 SKUs já finalizada, sem HTTP / Search /
JEV, execute após atualizar o master e **fechar ambos os Excels**.
Primeiro faça o pré-commit em cópias com as bases reais:

```powershell
& C:\p\venv\Scripts\python.exe .\pipeline_produtos.py --mode overnight-promote --promotion-dry-run --campaign-resume-dir "C:\p\Product_enricher\piloto_argentina\controlled_night\campaigns\20261111T140632Z_abf63101"
```

Exigir `"status": "DRY_RUN_PASS"` e
`"canonical_files_unchanged": true` antes da promoção. O pré-commit
também verifica todos os registros humanos por linha física, sem
conflar variantes ANNOVI/BOSCH pela mesma business_key. Cópias antigas
de promoção interrompida são reconstruídas somente se os arquivos
oficiais permanecerem idênticos ao backup original.

Depois do pré-commit aprovado, execute a promoção real:

```powershell
& C:\p\venv\Scripts\python.exe .\pipeline_produtos.py --mode overnight-promote --campaign-resume-dir "C:\p\Product_enricher\piloto_argentina\controlled_night\campaigns\20261111T140632Z_abf63101"
```

### Recuperação dos dois pilotos históricos de 20 SKUs

Depois que a campanha dos 50 terminar a promoção e exibir
`promotion.status=COMMITTED`, é possível recuperar também os 40 SKUs
históricos sem executar Search/JEV:

```powershell
& C:\p\venv\Scripts\python.exe .\pipeline_produtos.py --mode overnight-promote `
  --legacy-run-dir "C:\p\Product_enricher\piloto_argentina\controlled_night\20261110T230416Z_48864869" `
  --legacy-run-dir "C:\p\Product_enricher\piloto_argentina\controlled_night\20261111T124902Z_e5dea5cd"
```

A execução exige dois relatórios auditados de 20 chaves distintas,
`RAW_OK=20` em cada, classificação executada e planilhas/SQLite
preservados. Produz um manifesto recuperável sob
`campaigns/legacy40_<hash>/` e realiza a mesma promoção canônica.
O piloto inicial produziu 17 linhas de classificação para 20 SKUs:
os três sem saída permanecem pendentes, não se inventa taxonomia. Casos
históricos `REVIEW_CACHE_EVIDENCE` recebem `Review Required=SIM`
e `Auto Eligible=NÃO` durante a importação, sem alterar o arquivo
original e sem aprovação humana. Caso a auditoria antiga não atenda aos
critérios de segurança, o processo será bloqueado antes da gravação.

**Não executar duas promoções simultâneas nem abrir Excel durante a
atualização.** Se uma promoção falhar, verificar seu backup/manifesto
antes de uma nova tentativa.

O comando importa os 50 RAW_OK, aplica as classificações disponíveis,
verifica chaves, audit PASS e registros protegidos; só então substitui
os arquivos canônicos. Um snapshot prévio dos arquivos fica sob
`campaigns/<ID>/promotion_backup/`, exclusivamente para recuperação
e prova de integridade. Se uma etapa falhar, a promoção é bloqueada;
**não repetir** sem avaliar o relatório/backup. O relatório final da
campanha traz `promotion.status = COMMITTED` quando a gravação terminou.

Campanhas futuras usando `--mode overnight --execute-live` fazem a mesma
consolidação automaticamente ao encerrar com `COMPLETE`; nenhuma promoção
parcial é autorizada. Não existe gasto adicional de APIs na promoção.

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
