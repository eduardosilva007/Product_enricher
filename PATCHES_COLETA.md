# Patches da COLETA (pipeline v3.9.5-hf3.1)

Aplique **depois** de rodar `auditoria_coleta.py` e confirmar que a categoria 4/5 é relevante.
Faça backup do arquivo antes.

---

## Patch 1 — A peneira (Source Sieve) também olha o raw_content que a Tavily já devolveu

**Problema:** hoje a identidade (marca+modelo) é julgada só no *título + trecho curto* do resultado.
Se o SKU só aparece no corpo da página, a fonte é descartada ANTES de o `raw_content`
(que você já pagou na mesma request) ser usado.

**Onde:** função `_source_sieve`.

Troque:

```python
        identity = _source_identity_for_result(brand, search_model, row, cfg)
        title = safe(row.get("title"))
```

por:

```python
        identity = _source_identity_for_result(brand, search_model, row, cfg)
        identity_from_raw = False
        raw_body = safe(row.get("raw_content"))
        if identity["level"] not in {"STRONG", "PROBABLE"} and raw_body:
            probe = dict(row)
            probe["content"] = f"{safe(row.get('content'))}\n{raw_body[:30000]}"
            deep = _source_identity_for_result(brand, search_model, probe, cfg)
            if deep["level"] in {"STRONG", "PROBABLE"}:
                identity = deep
                identity_from_raw = True
        title = safe(row.get("title"))
```

E logo depois de `flags: List[str] = []` (dentro do mesmo `for row in rows:`), adicione:

```python
        if identity_from_raw:
            flags.append("IDENTITY_FROM_RAW_CONTENT")
```

Os filtros de compatibilidade/related e o isolamento do bloco do SKU continuam valendo depois,
então o patch só impede a morte precoce da fonte.

---

## Patch 2 — Guardar uma janela do corpo da página no cache

**Problema:** `raw_content` é gravado como `None`. Quando você muda uma regra de curadoria,
a opção 6 (curar cache, 0 créditos) só enxerga os trechos curtos e não consegue reavaliar de verdade.

**Onde:** adicione a função abaixo em qualquer ponto antes de `search_product_once`:

```python
def _persist_raw_window(text: str, model: str, cfg: Dict[str, Any], limit: int = 5000) -> str:
    """Guarda só a janela em volta do SKU (ou o início), para re-curar sem pagar de novo."""
    text = safe(text)
    if len(text) <= limit:
        return text
    spans = _target_spans(text, model, cfg)
    if not spans:
        return text[:1500]
    left = max(0, spans[0][0] - limit // 2)
    return text[left:left + limit]
```

Em `search_product_once`, troque:

```python
    persisted_results: List[Dict[str, Any]] = []
    for row in normalized:
        r = dict(row)
        r["_raw_content_available"] = bool(safe(r.get("raw_content")))
        r["raw_content"] = None
        persisted_results.append(r)
```

por:

```python
    persisted_results: List[Dict[str, Any]] = []
    for row in normalized:
        r = dict(row)
        raw_text = safe(r.get("raw_content"))
        r["_raw_content_available"] = bool(raw_text)
        r["raw_content"] = _persist_raw_window(raw_text, search_model, cfg) or None
        persisted_results.append(r)
```

**Custo:** até ~5 KB por resultado (~50 KB por produto). Para 15 mil produtos, algumas centenas de MB
no SQLite. Reduza `limit` se precisar.

**Importante:** isso vale só para coletas NOVAS. O que já está no cache continua sem corpo.
Como o `raw_hash` muda, os checkpoints desses produtos serão refeitos (comportamento esperado).

---

## Teste de query (sem código) — config

Em `pipeline_config.json`, em `collection.query_template`, hoje:

```
{brand} {search_model}
```

Teste em ~100 produtos que falharam (1 crédito cada), com `--max-search-new 100 --recollect-low-evidence`:

```
{brand} "{search_model}"
```

Aspas forçam o buscador a tratar o SKU como termo exato. Compare a categoria 0 antes e depois
rodando `auditoria_coleta.py` de novo. Só adote se a taxa melhorar.
