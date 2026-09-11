# Product Enricher — R9.4 RC4

**Release Candidate para piloto controlado. Não considerar STABLE até validação humana do lote piloto.**

## Arquitetura

Tavily RAW/cache → Stage 0 Evidence Gate → Stage 1 regras determinísticas → Stage 1.5 Candidate Funnel → NVIDIA → Groq → OpenRouter → taxonomia determinística.

### Stage 0 — Evidence Gate
- não classifica o produto;
- valida identidade Marca + Modelo e qualidade da fonte;
- usa Modelo original preservado + `search_model` derivado;
- aceita variações controladas de separadores, revisão/sufixos e SKUs embutidos;
- não usa `exact_match` no Tavily;
- separa `LOW_TARGET_EVIDENCE` de `TAXONOMY_GAP_NO_ALIAS`;
- resultados WEAK/CONTEXTUAL não sustentam TYPE/POWER sozinhos.

### Stage 1 — Regras determinísticas
- PRODUCT_TYPE nasce somente de aliases/regras do JSON;
- negativos, especificidade, componentes e colisões são executados no Python;
- Category General/FAMILY nunca são escolhidos pela LLM.

### Stage 1.5 — Candidate Funnel
- reduz os candidatos antes da LLM;
- máximo de 4 candidatos;
- janela relativa de score = 0.45;
- identidade mínima = PROBABLE;
- exige TITLE ou suporte suficiente entre fontes;
- 1 candidato bem sustentado pode ser resolvido sem LLM;
- excesso de candidatos/multi-produto bloqueia chamada para evitar arbitragem de ruído.

### LLM
Rota padrão:
1. NVIDIA `nvidia/nemotron-3.5-lightning-30b-a3b`
2. Groq `openai/gpt-oss-120b`
3. OpenRouter `openai/gpt-oss-120b:free`

A LLM atua como **árbitro**, não como classificador aberto. Ela recebe apenas candidatos sobreviventes do Stage 1.5 e não pode criar PRODUCT_TYPE, Category General ou FAMILY.

## Tavily / nova coleta
- `exact_match=false`;
- query primária: `{brand} {search_model} product specifications`;
- fallback apenas quando não há evidência target suficiente;
- query fallback: `{brand} {search_model} product details manual technical data`;
- recoleta seletiva para RAW vazio/ruim;
- cache antigo é preservado em `tavily_cache_history` antes de substituição.

## Proteções preservadas
- `Revisão B.I=OK` = HARD LOCK absoluto;
- Manual não é sobrescrito;
- Opção 4 = recuperação/classificação com cache, sem Tavily;
- MERCHANDISING removido;
- `EARTH AUGER → DRILLERS | DRILLERS`;
- `UTILITY SCISSORS` consolidado em `UTILITY SHEARS`;
- `MITRE SAW` consolidado em `MITER SAW`;
- `RIVETER` consolidado em `RIVET TOOL`;
- `CHARGER → PARTS & ACCESSORIES | ACCESSORIES`;
- `OTHERS | OTHERS` proibido pelo validador.

## Validação executada em 11/09/2026

```text
Pipeline:       3.9.4-rc4
Rules:          2026-09-11-v3.0.4-stage0-funnel-taxonomy-cleanup-r9.4-rc4
semantic_hash:  ff98ea9e4051
quality_hash:   bb8a834d0bbb

Python compile:             OK
Rules/schema validation:    OK
Synthetic gates:            48/48
Identity gates:             14/14
Real teacher executáveis:   22/22
Reference-only:             12
Cache validado:             3430 produtos
Variantes mescladas:        30
PRODUCT_TYPES:              132
POWER_TYPES:                7
```

Replay offline do cohort real de 236 itens problemáticos, sem Tavily e sem LLM:

```text
RESOLVED_RULE                 99
RESOLVED_FUNNEL               19
LOW_TARGET_EVIDENCE           88
MULTI_PRODUCT_TARGET          19
TAXONOMY_GAP_NO_ALIAS          7
NO_LLM_ELIGIBLE_TARGET_EVIDENCE 1
LLM realmente elegíveis        3
TOTAL                        236
```

Ou seja: **118/236 foram resolvidos deterministicamente e apenas 3 precisariam de LLM nesse replay**.

O replay integral dos 3430 itens foi iniciado neste ambiente, mas excedeu a janela de execução disponível; por isso a versão permanece RC e deve ser validada em piloto antes de escalar.

## Piloto recomendado

1. Substituir somente `pipeline_produtos.py`, `regras_negocio.json`, `pipeline_config.json` e `README.md`.
2. Não substituir `.env`, `cache_tavily.sqlite`, `pipeline_v3.sqlite`, Base Final, Consolidado ou backups.
3. Executar a **Opção 4** com limite de **30** itens.
4. Confirmar no início:
   - Pipeline `3.9.4-rc4`;
   - Rules `...v3.0.4...rc4`;
   - `QUALITY GATES OK`;
   - `tavily_calls=0`.
5. Revisar os 30 produtos antes de liberar o restante dos pendentes.
6. Só depois retomar nova coleta e o lote de ~1000 produtos.

## Git

Commit sugerido:

`R9.4 RC4 - Stage0 evidence gate, candidate funnel e taxonomia revisada`

Não versionar `.env`, `*.sqlite`, Excel operacional ou backups.
