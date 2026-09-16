# R9.5 HF1 — Collection Quality Gate + Target Isolation + Title Evidence

## Objetivo

Fortalecer a etapa que alimenta todo o pipeline. A R9.5 mantém Stage 1, Stage 1.5, taxonomia e a política de LLM da R9.4/HF1, mas muda a coleta para que **somente evidência associada ao SKU alvo chegue à classificação**.

Princípio central:

> RAW pode conter ruído para auditoria; `curated_results` não pode.


## HF1 — Correção de títulos explícitos e Modelo operacional

A HF1 corrige uma classe real de falso-manual observada em modelos como `DJG02 355 220V`.
O `220V` é preservado no campo Modelo original, mas removido somente do `Search Model`/identidade quando aparece como especificação técnica terminal e o núcleo anterior continua sendo um SKU estruturado.

Exemplo:

```text
Modelo original: DJG02 355 220V
Search Model:    DJG02 355
Fonte:           Dongcheng DJG02-355 Electric Cut Off Machine
Resultado:       CUT-OFF SAW
```

A regra é conservadora: `GSA 18V 32` continua inteiro porque a tensão está no meio do identificador e não como atributo terminal.

Também foram fechados gaps lexicais comprovados: `cut off machine` -> `CUT-OFF SAW`, `oscillating multi tool` -> `OSCILLATING MULTI-TOOL` e `screwdriver` -> `SCREWDRIVER`. Separadores como hífen/barra/espaço passam a ser equivalentes somente entre os mesmos tokens, sem fuzzy matching.

Um título descritivo sem vínculo com o SKU **não** é autoaprovado. Por exemplo, `Dongcheng 1300W Electric Reciprocating Saw` só resolve `DJF02 30` quando o mesmo SKU também está ancorado em URL/BODY isolado. Isso evita usar a descrição correta de um produto errado.

### Cache legado

Com `collection_quality.require_curated_evidence_for_classification=true`, cache antigo sem `_collection` não pode mais pular a R9.5. Ele recebe `PENDING_CURATION / LEGACY_CACHE_NOT_CURATED`; execute a opção 6 (0 créditos) ou uma nova coleta antes da classificação.

## Fluxo R9.5

```text
Base de Produtos
  ↓
Search Model derivado (Marca/Modelo originais preservados)
  ↓
Tavily Search ABERTO
  - sem include_domains
  - sem country filter
  - sem language filter
  - exact_match = false
  - auto_parameters = false
  ↓
Identity Gate (Marca + SKU + campo do match)
  ↓
Tavily Extract nas URLs elegíveis
  ↓
Target Isolation Funnel
  - página dedicada: limpa a cauda de related products
  - catálogo/listagem: isola o bloco/linha do SKU
  - outro SKU vira fronteira e não contamina o target
  - compatibilidade/related-only são rejeitados
  ↓
Collection Quality Gate
  ↓
_collection.curated_results
  ↓
Stage 1 → Stage 1.5 → NVIDIA/Groq → Taxonomia
```

## Search e Extract

A Tavily documenta `chunks_per_source` no Search (1–3 chunks, até 500 caracteres por chunk) e no Extract (1–5 chunks). O Extract aceita `query`, usada para reranquear os chunks da URL. A R9.5 usa esse fluxo para localizar o SKU sem induzir TYPE.

Configuração inicial:

- Search PRIMARY: `basic`, 2 chunks, 5 resultados.
- Search FALLBACK: `basic` se a evidência ainda não estiver pronta.
- Search RESCUE: `advanced` somente no residual.
- Extract: `basic`, 3 chunks por URL.
- Extract Advanced: retry seletivo apenas quando catálogo/tabela contém o SKU mas o Basic não permitiu isolá-lo.
- Tavily score: apenas ordenação secundária; nunca prova identidade.

Documentação usada:
- https://docs.tavily.com/documentation/api-reference/endpoint/search
- https://docs.tavily.com/documentation/api-reference/endpoint/extract
- https://docs.tavily.com/documentation/best-practices/best-practices-extract

## Estados de fonte

- `ACCEPT_TARGET_CLEAN`: página dedicada ao SKU, sem contaminação estrutural detectada.
- `ACCEPT_TARGET_ISOLATED`: página multi-produto/catálogo, mas o bloco do SKU foi isolado com sucesso.
- `REJECT_WRONG_MODEL`: SKU alvo não aparece na evidência.
- `REJECT_COMPATIBILITY_ONLY`: SKU aparece apenas como compatibilidade/substituição.
- `REJECT_RELATED_ONLY`: SKU aparece apenas em related products.
- `REJECT_GENERIC_NO_TARGET_BLOCK`: página genérica sem bloco do SKU recuperável.
- `REJECT_NON_ISOLATABLE`: SKU aparece, mas o texto ainda mistura outros SKUs após a tentativa de isolamento.
- `REJECT_WEAK_IDENTITY`: reservado para políticas futuras de identidade fraca.
- `REJECT_EXTRACT_FAILED`: reservado para falha explícita de extração.

## Estados do produto após coleta

- `READY_FOR_CLASSIFICATION`
- `PARTIAL_TARGET_EVIDENCE`
- `LOW_TARGET_EVIDENCE`
- `NO_TARGET_EVIDENCE`

`READY_FOR_CLASSIFICATION` ocorre quando há fonte STRONG suficiente, duas fontes PROBABLE independentes ou um bloco multi-produto isolado de forma exata conforme a policy.

## RAW x Curated

O cache continua preservando `results` do Search para auditoria. A R9.5 acrescenta `_collection`, que guarda:

- `curated_results`
- `source_audit`
- `query_audit`
- `raw_extract_results`
- `credits`
- `state`

Quando `_collection` existe, o Stage 1 **não faz fallback para `results`**. Se `curated_results` estiver vazio, a classificação recebe zero fontes e permanece pendente em vez de consumir RAW contaminado.

## Catálogos multi-produto

Exemplo:

```text
TOTAL Power Tools Catalogue
TL5508216 planer 550W
TGSLI20126 vacuum cleaner 20V
TDLI20012 drill 20V
```

Para `TL5508216`, a fonte não é descartada. O Target Isolation Funnel libera somente:

```text
TL5508216 planer 550W
```

`TGSLI20126`, `TDLI20012`, `vacuum cleaner`, `drill` e seus atributos não chegam ao Stage 1 daquele produto.

## Curadoria do cache atual sem gastar Tavily

Nova opção do menu:

```text
6 - CURAR CACHE LEGADO R9.5 (SEM TAVILY / 0 CRÉDITOS)
```

Ela usa `raw_content`/`content` já persistidos para criar `_collection.curated_results`, sem nova chamada de Search ou Extract. Cada atualização mantém a versão anterior em `tavily_cache_history`.

Sugestão operacional:

1. Fazer backup dos arquivos atuais.
2. Rodar a opção 6 com um piloto pequeno (ex.: 100 produtos).
3. Auditar `Curadoria_Cache_R9_5.xlsx`.
4. Rodar a coleta R9.5 apenas para os que continuarem com evidência insuficiente.
5. Auditar `Coleta_Tavily_R9_5.xlsx`.
6. Só então rodar a classificação.

Observação: a curadoria altera o `raw_hash` semântico porque passa a existir uma camada de evidência curada. O baseline Excel não é modificado pela opção 6. `Revisão B.I=OK` continua HARD LOCK na classificação.

## Relatórios

`Coleta_Tavily_R9_5.xlsx` terá:

- `Resumo`: estados, decisões e créditos observados.
- `Produtos`: estado final da coleta por Marca/Modelo.
- `Fontes`: decisão e motivo por URL.
- `Queries`: PRIMARY/FALLBACK/RESCUE e estado após cada etapa.

`Curadoria_Cache_R9_5.xlsx` usa a mesma estrutura, com créditos Tavily iguais a zero.

## Arquivos do pacote

- `pipeline_produtos.py`
- `pipeline_config.json`
- `collection_quality_policy.json`
- `test_collection_quality.py`
- `README.md`
- `regras_negocio.json` — atualizado na HF1 com aliases/tipo/regressões controladas para os gaps comprovados.

## Validações executadas

- `py_compile`: OK.
- `Rules.validate()`: 0 erros.
- Quality gates HF1: 58/58 synthetic, 16/16 identity, 22/22 real explicit.
- Fonte dedicada: aceita.
- Catálogo multi-produto: target isolado; outros SKUs removidos.
- Compatibility-only: rejeitado.
- Related-only: rejeitado.
- Página dedicada com related footer: footer contaminante removido.
- `_collection.curated_results=[]`: Stage 1 não volta ao RAW sujo.

## SUMMARY

`feat: add R9.5 collection quality gate and target isolation funnel`

## DESCRIPTION

- Keeps product search fully open with no domain, country or strict language filtering and exact_match disabled.
- Splits discovery from evidence consumption using Tavily Search followed by selective Tavily Extract.
- Adds deterministic SKU identity validation before TYPE/POWER detection.
- Adds Target Isolation Funnel for catalogues, listings and multi-product pages instead of discarding them by default.
- Prevents other SKUs, related products and compatibility-only references from contaminating the target product evidence.
- Stores raw Search/Extract data for audit while exposing only curated_results to Stage 1.
- Adds selective Advanced Extract retry for non-isolatable catalogue/table sources.
- Adds PRIMARY, FALLBACK and ADVANCED RESCUE search stages without injecting guessed product type into queries.
- Adds collection states, source decisions, query audit and Tavily credit telemetry.
- Adds zero-credit cache curation mode to reuse existing raw_content before spending new Tavily credits.
- Preserves Revisão B.I=OK as an absolute HARD LOCK.
- Preserves R9.4 deterministic classification rules, Stage 1.5 Candidate Funnel and NVIDIA/Groq routing.

## R9.5 HF2 — Evidence-driven Taxonomy

HF2 fecha o principal erro observado na amostra manual: `WATERPUMP` e `LIFT PUMP` nao podem mais definir uma FAMILY generica apenas pelo TYPE.

Fluxo:

```text
TYPE resolvido e target-anchored
        ↓
TAXONOMY_EVIDENCE_RULES
        ↓
subtipo comprovado?
  SIM → Category/FAMILY específica
  NÃO → REVIEW / TAXONOMY_UNMAPPED
```

O TYPE de negocio permanece `WATERPUMP` ou `LIFT PUMP`. Submersible, peripheral, circulation, centrifugal, pool, jet, priming e booster são tratados como refinamentos taxonomicos, nao como TYPE livre da LLM.

A regra `WATERPUMPS | WATERPUMP` foi removida do caminho automatico. Revisao B.I=OK continua HARD LOCK.

## HF3 — Recovery Ladder

HF3 corrige o comportamento `cache existe -> mas nada útil chega ao classificador`. A recuperação agora segue esta ordem:

```text
curated evidence
  -> target-anchored Search title (TITLE_ONLY_RESCUE)
  -> deterministic TYPE aliases
  -> taxonomy consensus when TYPE remains ambiguous
  -> LLM only when candidate policy allows
  -> selective recollection for residual evidence gaps
```

`TITLE_ONLY_RESCUE` nunca usa BODY RAW. Ele exige identidade `STRONG` ou `PROBABLE` no título/URL e bloqueia fontes explicitamente marcadas como wrong-model, compatibility-only ou related-only.

A Base Final passa a expor `Título Referência` e `Próxima Ação`. `Título Real` continua sendo a evidência promovida; `Título Referência` é apoio auditável para o usuário quando a evidência não pode ser promovida.

A opção 4 agora reporta também `Collection State`, `Raw Search Results` e `Curated Sources`, evitando interpretar `RAW_OK` como sinônimo de evidência pronta para classificação.
