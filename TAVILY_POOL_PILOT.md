# Piloto Tavily-only — 2026-11-19

## Escopo

- Busca somente Tavily com `--tavily-pool`; demais provedores não são selecionados nessa rota.
- Aceita até 20 configurações de contas com **uso explicitamente autorizado**, sem multiplicação implícita de franquias.
- A configuração não faz a API aceitar práticas contrárias aos termos do serviço: use apenas chaves e orçamentos permitidos pela Tavily.
- Cada SKU tem uma única reserva persistida antes do HTTP; alternância de chave só é aplicada ao **próximo SKU**, quando se atinge orçamento configurado de uma conta.
- Respostas `LIMIT/AUTH/SCHEMA` interrompem a campanha sem rotação para contornar bloqueio de provedor.
- `429` **não** aciona troca de conta nem retry; procure o limite e liberação no painel do provedor.
- A preparação completa da Base Final foi excluída **apenas de `collect --tavily-pool`**. O resultado da coleta entra no cache SQLite e relatório, não regrava Master/Final automaticamente. Use a sincronização oficial separadamente após revisão.
- Nenhuma reserva histórica do Serper é liberada nessa rota. Auditar antes de corrigir os 5.011 bloqueios.
- Primeira etapa obrigatória: testes offline, depois piloto real pequeno, merge somente após qualidade e integridade comprovadas.

## Configuração .env (NÃO versionar chaves)

Cada par chave/orçamento deve representar autorização real. Exemplos apenas:

```dotenv
TAVILY_API_KEY_1=COLOQUE_SUA_CHAVE_AQUI
TAVILY_LIMIT_1=3
TAVILY_API_KEY_2=COLOQUE_SUA_OUTRA_CHAVE_AQUI
TAVILY_LIMIT_2=3
```

Repetir o padrão até `TAVILY_API_KEY_20` e `TAVILY_LIMIT_20`, somente se as contas forem autorizadas. Os limites representam **número de solicitações**, não garantia de saldo em créditos. Fixar orçamento menor que o saldo real e monitorar o painel da Tavily. Para chaves que compartilham uma única franquia, informar o mesmo `TAVILY_BILLING_GROUP_N` e o mesmo `TAVILY_LIMIT_N`: nesse caso o teto é compartilhado, não somado.

Durante o piloto, use um orçamento mínimo compatível com os testes, sem aumentar os limites automaticamente.

## Execução — mesmo diretório C:\\p\\Product_enricher

```powershell
cd C:\p\Product_enricher
# Buscar a branch do piloto sem trocar de pasta ou criar worktree.
git -c gc.auto=0 fetch origin feature/tavily-only-authorized-pool-20261119
# Conferir alterações locais ANTES de restaurar:
git status --short
git diff -- pipeline_produtos.py
# Se não houver trabalho local que precise ser preservado:
git restore --source=FETCH_HEAD --worktree -- pipeline_produtos.py
# Obter também os testes do piloto (restaura somente o caminho informado):
git restore --source=FETCH_HEAD --worktree -- tests/test_tavily_authorized_pool.py

& "C:\p\venv\Scripts\python.exe" -m py_compile pipeline_produtos.py tests/test_tavily_authorized_pool.py
& "C:\p\venv\Scripts\python.exe" -m unittest discover -s tests -p "test_tavily_authorized_pool.py" -v
& "C:\p\venv\Scripts\python.exe" pipeline_produtos.py --mode tavily-pool-doctor
```

Depois de tests/doctor PASS, o piloto real, **com consumo de créditos e confirmação explícita**, é:

```powershell
& "C:\p\venv\Scripts\python.exe" pipeline_produtos.py --mode collect --search-provider tavily --tavily-pool --max-search-new 3
```

Digite `N` para recoleta de `RAW_EMPTY`; confira as 3 linhas elegíveis e a quantidade de contas, então digite `CONFIRMAR` se o orçamento e as autorizações estiverem corretos.

## Critérios de aceitação antes do merge

1. Testes offline verdes sem rede; doctor mostra contas/budgets e não expõe chaves.
2. Não existem requisições a Serper/Bright Data/ScraperAPI no modo pool.
3. Uma reserva por SKU e retomada sem repetição, confirmadas em SQLite.
4. Resultado persistido no cache SQLite central e relatório `Coleta_Tavily_Pool.xlsx`.
5. Sem alteração automática no Master, Base Final e classificações humanas durante `collect --tavily-pool`.
6. Três SKUs do piloto coletados com evidência auditável; erros de quota pausam sem trocar de chave para contornar bloqueio.
7. Medir tempo de preparação e comparar com o piloto anterior (~28 minutos até o planejamento); não assumir ganho até os logs comprovarem.
8. Auditoria independente das 5.011 reservas históricas antes de liberar qualquer item.

## Limitações conscientes

O doctor contabiliza reservas por conta em SQLite; ele **não consulta créditos reais** da Tavily. As contas devem ser autorizadas pelo provedor e o limite configurado deve permanecer conservador. A seleção é sequencial por conta e não usa rotação para simular usuários ou evitar detecção. 
