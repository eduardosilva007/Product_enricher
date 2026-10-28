# Piloto V4.1 — BGE-M3 híbrido x BGE-M3 + GLiNER

Branch: `pilot/v41-bge-gliner-ab`

## Objetivo

Medir, no mesmo cohort e sobre a mesma evidência:

- **Braço A — SEM_GLINER:** JSON-LD + identidade MARCA/MODELO + BGE-M3 local (dense+sparse+ColBERT) + BGE reranker.
- **Braço B — COM_GLINER:** exatamente o mesmo pipeline, acrescentando somente as features extraídas pelo GLiNER antes do BGE.

O piloto **não grava Consolidado/Base Final**, **não chama Search API** e **não chama LLM**. JSON-LD é obtido por fetch HTTP local das URLs já existentes no cache e fica cacheado em `pilot_v41_jsonld_cache.json`.

## Identidade forte do piloto

`TARGET_CONFIRMED` exige pelo menos um destes caminhos:

1. **MARCA + MODELO em campos primários** da mesma fonte (TITLE/URL/domínio), usando correspondência estrutural de SKU, sem fuzzy; ou
2. **JSON-LD Product** com `brand` compatível + `sku/mpn/model/productID` compatível com o modelo alvo.

Modelo sozinho não confirma identidade. Isso evita colisões como ADV3 AA7000 vs American Airlines AA7000 e ADV3 AA8500 vs Akai AA-8500.

A identidade é separada da confiança de classificação. Um produto pode estar inequivocamente identificado e ainda assim ter TYPE ambíguo.

## Preparação

```powershell
git checkout pilot/v41-bge-gliner-ab
git pull

python -m pip install -r requirements-pilot-v41.txt
```

O BGE-M3 e o BGE reranker continuam usando os caminhos locais já existentes em `pipeline_config.json`:

- `C:\p\models\bge-m3`
- `C:\p\models\bge-reranker-v2-m3`

Nenhuma API BGE é usada.

Para GLiNER, aponte para um checkpoint local, se já existir:

```powershell
$env:GLINER_MODEL_PATH="C:\caminho\para\gliner2-multi"
```

Ou, se aceitar baixar o checkpoint uma vez:

```powershell
$env:GLINER_MODEL_PATH="fastino/gliner2-multi-v1"
```

## Rodar o A/B

Piloto pequeno:

```powershell
python pilot_v41_ab.py --input "Base de Produtos.xlsx" --max-items 20
```

Piloto maior:

```powershell
python pilot_v41_ab.py --input "Base de Produtos.xlsx" --max-items 100
```

Se não informar `--input`, o script usa o catálogo do cache.

Saída padrão:

`Piloto_V41_BGE_GLiNER_AB.xlsx`

Abas:

- **Resumo** — cobertura, accuracy quando houver Gold validado e tempos médios.
- **Sem GLiNER** — resultado detalhado do braço A.
- **Com GLiNER** — resultado detalhado do braço B.
- **Comparativo** — delta produto a produto, incluindo `GLiNER Ganhou`, `GLiNER Perdeu` e `Delta Tempo s`.
- **Skipped** — itens sem RAW/evidência utilizável.

## O que conta como Gold no relatório

O piloto só usa a classificação existente como Gold quando a linha estiver explicitamente validada por:

- `Revisão B.I = OK`; ou
- `Status = Manual`.

Linhas Script não validadas não entram no cálculo de accuracy.

## Gate local do piloto

O campo `Local Auto Candidate=SIM` é **experimental**, não altera a base. Ele exige:

- identidade `TARGET_CONFIRMED`;
- Category/FAMILY fechados e válidos;
- reranker probability >= 0.55;
- reranker margin >= 0.15;
- hybrid margin >= 0.02.

Os thresholds servem para medir cobertura/risco e serão recalibrados com o Gold ampliado antes de virarem regra produtiva.

## Critério para manter GLiNER

GLiNER só entra na V4.1 produtiva se o A/B mostrar benefício mensurável:

1. aumento de accuracy no Gold validado; ou
2. ganho claro em casos que o braço sem GLiNER errou, sem criar regressões equivalentes;
3. custo adicional de latência aceitável.

Se o ganho for nulo/marginal, fica fora do caminho normal.
