# Bright Data multizone — operação segura (2026-11-16)

## Origem
Esta branch preserva integralmente o \`pipeline_produtos.py\` enviado pelo usuário como versão de referência, acrescido de correções pontuais de roteamento, credenciais, validação e parada segura.

## Configuração (exemplo; nunca versionar chaves)
\`\`\`dotenv
BRIGHTDATA_API_KEY_1=<token-da-zona-1>
BRIGHTDATA_ZONE_1=serp_api1
BRIGHTDATA_LIMIT_1=4990
BRIGHTDATA_API_KEY_2=<token-da-zona-2>
BRIGHTDATA_ZONE_2=serp_api2
BRIGHTDATA_LIMIT_2=5000
\`\`\`
As duas chaves devem ser distintas e cada uma deve ter acesso à zona correspondente. Se o saldo for compartilhado, configure os grupos/limites conscientemente; não assuma duas cotas separadas. Remova nomes repetidos do \`.env\` (inclusive duplicações não indexadas).

## Preflight
\`\`\`powershell
& "C:\p\venv\Scripts\python.exe" .\tools\brightdata_preflight.py --slot 1
& "C:\p\venv\Scripts\python.exe" .\tools\brightdata_preflight.py --slot 2
\`\`\`
Esses comandos não usam HTTP nem gravam no SQLite.
\`\`\`powershell
& "C:\p\venv\Scripts\python.exe" .\tools\brightdata_preflight.py --slot 1 --live
\`\`\`
Somente \`--live\` autoriza 1 POST, potencialmente cobrado, fora do banco de tentativas. Executar o mesmo teste no slot 2 somente se necessário.

## Testes offline
\`\`\`powershell
& "C:\p\venv\Scripts\python.exe" -m unittest discover -s tests -p "test_brightdata_multizone_safety.py" -v
\`\`\`
## Segurança
- Sem retry automático de SKU, sem gravação de erros operacionais no RAW.
- HTTP 400 \`zone not found\` é \`BRIGHTDATA_CONFIG\`: desabilita a conta e pausa a campanha.
- Histórico de 5.000 tentativas anteriores não é apagado nem reautorizado automaticamente.
- Limites de \`*_LIMIT_N\` não significam crédito disponível: o ledger persiste reservas anteriores.
- \`main\` não é alterada por esta branch.
