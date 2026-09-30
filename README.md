# Product Enricher — R9.7 Cache-First Free-Tier

## Objetivo

R9.7 muda o foco para **escala com custo operacional zero no caminho principal**.
A nova opção reaproveita o `cache_tavily.sqlite` já coletado e não faz nova busca nem chama LLM/API paga.

Fluxo novo:

```text
cache_tavily.sqlite
        ↓
Evidence Pack por Marca + Modelo
        ↓
Identity Gate + Cross-Brand Gate
        ↓
Stage 1 determinístico
        ↓
Local Taxonomy Retrieval
        ↓
regras_negocio.json
        ↓
proposta / aceite determinístico / gap
```

## O que muda

### 1. Cache-first de verdade

A opção 8 lê o cache Tavily existente e usa todos os resultados disponíveis para construir um pacote compacto de evidência por produto. O cache já pago passa a ser a matéria-prima principal.

### 2. Bloqueio de fonte contaminada por outra marca

Se o alvo for `HYUNDAI | 42004 000104` e o título da fonte trouxer explicitamente `TCL ... 42004-000104`, a fonte é marcada como `CROSS_BRAND` e não pode sustentar aceite automático.

Ela pode permanecer como `CONTEXT_ONLY` para revisão, mas não vira prova de identidade.

### 3. Título genérico não vira Título Real

Títulos como:

```text
www.XYtronic-USA.com
Home
Official Website
Products
```

são identificados como genéricos. Quando houver conteúdo útil no cache, o relatório usa um trecho target-aware da evidência no lugar do domínio.

### 4. Recuperação taxonômica local

Ordem de backend:

1. `sentence-transformers` local, se o pacote e o modelo já estiverem instalados;
2. `scikit-learn` TF-IDF local;
3. fallback lexical em Python puro.

Nenhum deles exige API paga.

Modelo recomendado para embeddings locais:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

O pipeline usa `local_only=true` por padrão para não iniciar download silencioso em notebook corporativo.

### 5. Acceptance policy

Por padrão, **similaridade semântica sozinha não autoaprova** (`auto_accept=false`).

Existe uma exceção determinística segura:

```text
IDENTITY = STRONG
+ sem conflito de marca
+ alias específico da taxonomia aparece literalmente na evidência
+ taxonomia válida
= LOCAL_EXACT_ALIAS_ACCEPTED
```

Isso permite ganhar escala sem transformar embeddings em fonte de verdade.

### 6. Checkpoint local

Os resultados são persistidos em `pipeline_v3.sqlite`, tabela `cache_first_results`, usando:

```text
business_key + raw_hash + rules_hash + retriever_signature
```

Se o processo for interrompido, resultados compatíveis podem ser reutilizados.

### 7. Evidence Packs reutilizáveis

Também é criado:

```text
Cache_Evidence_Packs_R9_7.jsonl
```

Cada linha contém Marca, Modelo, identidade, fontes, trechos de evidência e flags. Esse JSONL pode futuramente alimentar LangExtract, uma LLM em batch ou outro modelo sem reler o SQLite inteiro.

## Taxonomias adicionadas

R9.7 acrescenta gaps observados no piloto:

- `CROSS FLOW FAN` → `OTHERS | CROSS FLOW FAN`
- `PROPELLER FAN` → `OTHERS | PROPELLER FAN`
- `SOLDERING STATION` → `OTHERS | SOLDERING STATION`
- `WINDSCREEN WASHER RESERVOIR` → `OTHERS | WINDSCREEN WASHER RESERVOIR`

Também bloqueia `washer fluid reservoir` de ser interpretado como `WINDSCREEN WASHER PUMP`.

## Como instalar

Caminho mínimo, totalmente local:

```powershell
pip install -r requirements.txt
```

Isso inclui `scikit-learn` e já permite rodar o novo piloto.

### Embeddings multilíngues locais — opcional

```powershell
pip install -r requirements_embeddings_optional.txt
```

O pipeline **não baixa o modelo automaticamente**. Para baixar uma única vez quando a rede permitir:

```powershell
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"
```

Depois disso, com o modelo no cache local, a opção 8 passa a preferi-lo automaticamente.

Se a instalação/modelo não estiver disponível, o pipeline continua normalmente com TF-IDF.

## Como rodar

Primeiro:

```powershell
python pipeline_produtos.py --mode validate
```

Depois:

```powershell
python pipeline_produtos.py
```

Escolha:

```text
8 - CACHE-FIRST FREE-TIER LOCAL (0 BUSCA / 0 LLM / NÃO ALTERA BASE)
```

Para o primeiro comparativo use `100` itens.

## Saídas

```text
Piloto_Cache_First_Free_Tier_R9_7.xlsx
Cache_Evidence_Packs_R9_7.jsonl
```

O Excel mostra, entre outros:

- Marca / Modelo
- Stage1 State
- Best Identity
- fontes trusted/context
- cross-brand count
- backend local
- Suggested Type
- Matched Taxonomy Phrase
- Similarity Score / Margin
- Suggested Category General / FAMILY
- Título Real
- Evidence Extract / URL
- Decision
- Review Required

## Decisions

- `LOCAL_EXACT_ALIAS_ACCEPTED`: identidade STRONG + alias específico explícito; aceite determinístico local.
- `LOCAL_SEMANTIC_PROPOSAL`: candidato local útil, ainda requer validação.
- `LOCAL_LOW_SIGNAL`: cache tem evidência, mas a taxonomia local não encontrou sinal suficiente.
- `CACHE_IDENTITY_GAP`: existe conteúdo, mas ele não prova com segurança o Marca+Modelo alvo.
- `CACHE_INSUFFICIENT`: cache não possui texto suficiente para interpretar o produto.

## O que NÃO mudou

- 1 produto = no máximo 1 Search-provider request nos modos de coleta.
- Sem retry automático de Search.
- Sem segunda query automática.
- Sem provider failover na coleta.
- Sem Tavily Extract / Advanced.
- `Revisão B.I = OK` continua HARD LOCK.
- A opção 8 não grava Base Final nem Consolidado.
- Category General e FAMILY continuam controladas pelo `regras_negocio.json`.

## LangExtract

LangExtract continua interessante para uma segunda camada de extração grounded, mas **não foi colocado no caminho crítico da R9.7**. A opção 8 precisa funcionar sem Ollama, sem navegador, sem LLM e sem nova API. O `Cache_Evidence_Packs_R9_7.jsonl` já prepara os dados para um futuro adaptador LangExtract sem novo Search.
