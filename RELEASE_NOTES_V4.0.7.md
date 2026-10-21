# PIPELINE v4.0.7-evidence-cache-hardening

## Objetivo

Reduzir rejeição causada por evidência pobre/ruidosa sem aumentar o número de buscas por produto.

A v4.0.7 herda integralmente a correção de cohort da v4.0.6 e endurece a Collection R9.6p7 para formar um cache reutilizável por:

- classificação automática;
- CACHE_RESCUE/Clef;
- outra LLM via chat/API;
- double check humano.

## Contratos preservados

- Máximo de 1 Search request por produto.
- Sem retry automático, segunda query, provider failover, Tavily Extract ou Advanced.
- Clef, BGE-M3, reranker e thresholds de AUTO permanecem inalterados.
- Revisão B.I=OK continua HARD LOCK absoluto.
- Category General e FAMILY continuam fechados no `regras_negocio.json`.

## Melhorias implementadas

### 1. Evidence Cache v1

Antes de descartar o `raw_content`, a coleta materializa evidências compactas por fonte:

- title;
- URL/domain;
- provider score/rank;
- identidade;
- decision/quality flags;
- snippet limpo;
- evidence_excerpt target-anchored;
- hash da evidência;
- origem do conteúdo;
- tier de uso.

Tiers:

- `PRIMARY`: passou pelo Collection Quality Gate; pode alimentar classificação.
- `AUXILIARY_REVIEW_ONLY`: evidência reaproveitável para rescue/LLM, mas não recebe autoridade automática.
- `REJECTED`: mantida para auditoria, excluída de LLM rescue por padrão.

O corpo integral da página continua não persistido.

### 2. Limpeza semântica severa

Novo cleaner remove:

- HTML tags;
- script/style/noscript/svg/iframe;
- nav/header/footer/form quando presentes em HTML;
- entidades HTML;
- caracteres de controle;
- linhas boilerplate conhecidas;
- linhas duplicadas.

A limpeza é conservadora: não reescreve, resume nem inventa texto.

### 3. BODY Identity Rescue finalmente executável

A policy já declarava `body_identity_rescue`, mas a versão anterior não o executava.

Na v4.0.7:

- só roda quando TITLE/URL/snippet não chegam a PROBABLE;
- exige SKU no body;
- exige Marca próxima do Modelo;
- bloqueia compatibility-only e related-only;
- teto de identidade = PROBABLE;
- nunca produz STRONG/AUTO por si só.

### 4. Retenção target-anchored real

A policy também declarava retenção de janela target, porém o Python descartava o `raw_content` sem materializar essa camada.

Agora cada fonte pode persistir um `evidence_excerpt` de até 1200 caracteres, configurável, em torno do SKU alvo.

### 5. Recuradoria zero-custo

A opção de curar cache legado passa a materializar Evidence Cache v1 quando houver conteúdo suficiente no cache atual.

Não ocorre nova Search.

Caches históricos em que o `raw_content` já foi apagado só poderão aproveitar snippets e `curated_results` existentes; conteúdo perdido não é reconstruído/inventado.

### 6. Export para chat/LLM

Novo modo:

`--mode evidence-export`

Gera:

`Evidence_Bundle_v4_0_7.jsonl`

Cada linha contém produto, estado da coleta, métricas, fontes e um `llm_context` pronto para ser enviado a outra LLM.

O export faz zero Search e zero chamada Clef.

### 7. Release Contract fail-closed

A execução agora valida em conjunto:

- pipeline_version;
- collection_engine_version;
- collection_policy_version;
- rules_version;
- evidence_cache_schema.

Isso elimina misturas silenciosas como código v4.0.7 executando com config/policy de outra release.

## Gaps encontrados na versão anterior

1. `raw_content` era descartado integralmente depois do isolamento.
2. Não havia um pacote de evidência reutilizável para fallback.
3. O fallback HTML stdlib podia preservar menu/header/footer.
4. `body_identity_rescue` existia na policy, mas não era executado.
5. A policy dizia persistir janela target-anchored, mas o executor não a materializava.
6. Config, docstring, pipeline e regras podiam reportar versões diferentes.
7. Cache legado podia ser recurador, mas não gerava uma camada própria para LLM/rescue.

## Validação adicionada

`test_v407_evidence_cache.py` cobre:

- remoção de HTML/boilerplate e duplicatas;
- BODY rescue limitado a PROBABLE;
- bloqueio de compatibility-only;
- proibição de persistir raw_content integral;
- separação PRIMARY x REJECTED;
- fail-closed da policy de persistência.

## Estratégia para os 7.700 itens

Não alterar thresholds/taxonomia durante a medição.

Medir:

1. Collection State;
2. quantidade PRIMARY/AUXILIARY por produto;
3. Evidence chars por produto;
4. taxa de classificação normal;
5. pendentes após fluxo normal;
6. depois, CACHE_RESCUE em amostra controlada.

Meta operacional continua reduzir intervenção humana da faixa observada de ~60% para 30–35%.
