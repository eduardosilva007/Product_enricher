# Argentina — coleta multiprovider (branch experimental, NÃO produção)

O executor `pipeline_produtos.py` aceita `--auto-providers` e passa para o próximo **produto** usando outra credencial/provedor quando a conta esgota orçamento local ou retorna HTTP 401/403/429/432/433. Não faz fallback do **mesmo** produto. Cada tentativa é reservada no SQLite `pipeline_v3.sqlite` antes de chamar a API, e não é repetida automaticamente mesmo após timeout ou crash. Nunca colocar chaves nos logs.

## ATENÇÃO: branch main/local diferente
A branch do PR foi criada a partir de `main`, e o ambiente local informado usa `fix/jev-routing-audit-20261106`; **não** fazer switch, reset ou merge na cópia produtiva sem reconciliar essas diferenças. Os testes adicionais foram commitados, mas não executados no Windows após os novos commits.

## Configuração local (não commitar .env)
```dotenv
TAVILY_API_KEY=...
TAVILY_LIMIT_1=10
BRIGHTDATA_API_KEY=...
BRIGHTDATA_ZONE=...
BRIGHTDATA_LIMIT_1=10
BRIGHTDATA_API_KEY_2=...
BRIGHTDATA_LIMIT_2=10
SERPER_API_KEY=...
SERPER_LIMIT_1=10
```
Limites são contagens conservadoras de requests configuradas localmente (não saldo oficial). Se duas keys compartilham orçamento, configurar `BRIGHTDATA_BILLING_GROUP_1=grupo1` e `BRIGHTDATA_BILLING_GROUP_2=grupo1`; ambas devem usar o mesmo limite total compartilhado. O roteador usa inicialmente ordem Tavily → Bright1 → Bright2 → Serper. Apenas credenciais com key e LIMIT positivo ficam disponíveis.

## Comandos *quando código e dados locais estiverem verificados*
Em um worktree/clone separado:
```powershell
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_nightly_provider_router.py" -v
& C:\p\venv\Scripts\python.exe -m unittest discover -s tests -p "test_identity_ranking_experiment.py" -v
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode validate
& C:\p\venv\Scripts\python.exe pipeline_produtos.py --mode collect --auto-providers --max-search-new 10
```
`--recollect-empty` só mediante auditoria das 1.030 linhas de resposta vazia; a flag permite nova consulta que pode custar crédito. Por padrão RAW_EMPTY permanece fora da fila. Não iniciar coleta noturna em massa antes de testar o roteador com mocks de HTTP, verificar os custos reais e confirmar a persistência de títulos.

## Pendências de liberação
- Teste integrado HTTP 429/403/timeout sem segunda request, com conta real de poucos produtos.
- Controle de cota separado por data/período (atualmente global por arquivo SQLite; conservador).
- Bloqueio de invocação concorrente e plano de migração da tabela de reservas sem perder dados.
- Integração completa do experimento char n-gram/hard negatives ao funil; hoje módulo isolado.
- Corrigir persistência de título e distinção de RAW_EMPTY antes da escala.
