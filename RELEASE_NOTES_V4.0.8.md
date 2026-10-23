# v4.0.8 — Flow State & Classifier Hardening

## Motivo

A v4.0.7.3 resolveu o gap estrutural de identidade (ex.: MILWAUKEE 2446-21XC), mas os pilotos seguintes expuseram problemas de arquitetura no fluxo de classificação e baseline:

- o cohort de classificação era inferido do Excel atual, então um lote coletado com 18 itens podia virar 1 item se o arquivo de entrada fosse trocado depois;
- CLASSIFIED_CLEF / PROVÁVEL podia perder estado técnico no round-trip da Base Final e ser confundido com aceito;
- checkpoint dependia de PIPELINE_VERSION, então mudança só de Excel/baseline podia repetir classificação;
- páginas "MODEL Parts" podiam transformar o equipamento-pai em PART;
- ENGINE explícito no TITLE podia ser cancelado por "engine parts" no BODY;
- BGE podia reintroduzir PART semanticamente depois do Stage 1 bloquear corretamente;
- TAXONOMY_EVIDENCE_RULES específicas de outra marca/modelo vazavam para o universo global de ENGINE;
- Clef podia receber pares de taxonomia globalmente válidos, porém incompatíveis com o TYPE escolhido;
- Título Real podia preferir vídeo/página de reparo apesar de existir página oficial limpa.

Os seis casos Briggs observados no piloto viraram regressões obrigatórias do release.

## Arquitetura v4.0.8

### 1. Cohort Manifest imutável

A coleta grava `Collection_Cohort_Manifest_v4_0_8.json` contendo:
- schema_version;
- cohort_id estável;
- input SHA-256;
- business_keys do lote;
- provider;
- versão de coleta;
- versão semântica do classificador;
- Search candidates/completed;
- status RUNNING/COMPLETED/NO_SEARCH_NEEDED.

A classificação usa esse manifest por padrão e ignora alterações posteriores no Excel de entrada.

`--classification-use-current-input` existe apenas como override explícito.

### 2. Lifecycle explícito

Estados:
- PENDING_PROCESSING
- CLASSIFIED_PENDING_REVIEW
- ACCEPTED_AUTO
- ACCEPTED_HUMAN
- NEEDS_RECOLLECTION

`PROVÁVEL` é review-only e nunca baseline aceito sem Revisão B.I=OK.

Base Final preserva:
- Lifecycle State
- Resolution State
- Review Required
- Classification Identity
- Collection State
- Reason Code
- Pipeline Version
- Classification Semantic Version
- Collection Cohort ID
- Processed At

CLASSIFIED_PENDING_REVIEW entra em "processed" para não chamar Clef novamente, porém fica fora de "accepted".

### 3. Checkpoint semântico

Checkpoint usa:
`CLASSIFICATION_SEMANTIC_VERSION = 2026-10-23-classifier-v20`

e não mais `PIPELINE_VERSION`.

Mudança apenas de baseline/Excel/telemetria não invalida uma classificação semântica já concluída.

### 4. Source Purpose compartilhado

Collection e Classifier usam os mesmos sinais:
- PARTS_CATALOG_TITLE
- PARTS_DOCUMENTATION_TITLE
- REPAIR_TITLE

Página "MODEL Parts" identifica o equipamento-alvo, mas não prova que o próprio MODEL seja PART.

PART é removido do Stage 1 e também do pool semântico quando só existe contexto de catálogo de peças do equipamento-pai.

### 5. Negativos por campo

Negativo no TITLE continua bloqueando TYPE.

Negativo apenas no BODY não apaga TYPE físico explicitamente identificado no TITLE.

Exemplo:
"40U777 Engine" no TITLE + "engine parts available" no BODY continua ENGINE.

### 6. ENGINE / PETROL multilíngue

ENGINE ganhou aliases explícitos como:
- engine
- vertical engine
- v-twin engine
- moteur
- moteur essence

PETROL ganhou sinais:
- essence
- moteur essence
- moteur thermique
- 4 temps
- 2 temps

### 7. Identidade da coleta preservada

Fonte curada STRONG/PROBABLE não pode ser silenciosamente rebaixada na classificação por diferença entre Modelo original e Search Model.

Isso corrige o caso 613477, onde coleta STRONG chegava ao classificador como CONTEXTUAL/WEAK.

### 8. Taxonomia condicionada ao produto

`_taxonomy_pairs_for_type` respeita brand/model das TAXONOMY_EVIDENCE_RULES.

Regras específicas KOOP/DENNIS/MASALTA/TAVOL não vazam para Briggs.

O pool do Clef contém somente pares mapeados para os TYPEs candidatos no produto corrente.

Uma resposta Clef só é RESOLVED se:
- TYPE está entre candidatos;
- par é globalmente válido;
- par pertence ao TYPE para a marca/modelo atual.

### 9. Título Real com autoridade

Prioridade:
1. domínio da fabricante;
2. título sem parts/repair;
3. referência usada na classificação;
4. identidade;
5. score.

Página oficial limpa vence YouTube/repair/parts quando ambos identificam o SKU.

## Regressões obrigatórias

- 08P6020022H5A0096 → ENGINE, não PART; PETROL com "moteur essence/4 temps";
- 40U7770010 → ENGINE/PETROL, mesmo com "engine parts" no BODY;
- 44S9770046G1AF0001 → ENGINE/PETROL/ENGINES|ENGINE;
- 500 E → página oficial Briggs vence vídeo de reparo como Título Real;
- 550 EX → página "550ex Parts" não pode produzir PART nem ser reintroduzida como PART pelo BGE;
- 6134774205J1EB0001 → collection identity STRONG não pode virar WEAK; "Replacement Engine" gera ENGINE;
- CLASSIFIED_CLEF → CLASSIFIED_PENDING_REVIEW, não accepted;
- Revisão B.I=OK → ACCEPTED_HUMAN/HARD LOCK;
- cohort de 18 permanece 18 mesmo se o Excel depois tiver 1;
- cohort_id permanece estável durante a coleta;
- patch de baseline não muda checkpoint semântico;
- release contract, rules, collection policy, flow policy e identity contract validam juntos.

## Contratos preservados

- 1 Search request máximo/produto;
- sem retry automático;
- sem segunda Search;
- sem provider failover;
- sem Tavily Extract/Advanced;
- Structural SKU Core sem fuzzy/edit-distance;
- Revisão B.I=OK continua HARD LOCK absoluto;
- nenhum histórico BI_OK é alterado automaticamente;
- Evidence Cache permanece persistido sem full raw body.

## Versionamento

- Pipeline: `4.0.8-flow-state-classifier-hardening`
- Collection: `R9.6p8`
- Collection engine/policy: `2026-10-23-r9.6p8-source-purpose`
- Classifier semantic: `2026-10-23-classifier-v20`
- Prompt: `2026-10-23-v20-source-purpose-type-taxonomy-guard`
- Rules: `2026-10-23-v3.0.14-source-purpose-engine-guard`
- Cohort Manifest schema: `1`

## Política de promoção

Este release não deve ser mergeado por um único teste específico.

Pré-requisitos:
- py_compile verde;
- regressões v4.0.7, v4.0.7.1, v4.0.7.2, v4.0.7.3 e v4.0.8 verdes;
- JSON syntax verde;
- release/flow/rules/policy contracts verdes;
- revisão do diff main...feature sem arquivos não relacionados;
- PR único após todos os gates.
