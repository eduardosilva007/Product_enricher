# Product Enricher — R9.2

R9.2 volta ao **Stage 1 comprovadamente estável do R5/v3.7.1**, mantendo as proteções novas que não existiam naquele build.

## Arquitetura
`Tavily RAW -> candidatos JSON (Stage 1 R5) -> Groq -> NVIDIA -> OpenRouter -> validação estrita -> taxonomia JSON`

## O que foi mantido do R5 estável
- aliases/regras formam os candidatos;
- candidato único só é automático quando existe Marca+Modelo/Modelo na evidência ou repetição em fontes;
- múltiplos candidatos seguem para desempate;
- fonte contextual não vira aprovação automática.

## Proteções adicionadas
- LLM só pode escolher TYPE existente nos candidatos do Stage 1;
- LLM só pode escolher POWER já encontrado pelo Stage 1 ou UNKNOWN;
- TYPE resolvido pela LLM precisa ser sustentado pela fonte citada;
- fonte usada para RESOLVED precisa atingir identidade PROBABLE (modelo presente);
- sem candidato de TYPE = REVIEW sem chamada LLM;
- conflito de múltiplos tipos explícitos no mesmo título = REVIEW;
- Revisão B.I=OK = HARD LOCK absoluto;
- MERCHANDISING removido;
- regras + testes reais/sintéticos/identidade em um único `regras_negocio.json`.

## Operação
Para continuar o lote já coletado, use a opção 4. Não use `--reprocess` nos itens validados.

Commit sugerido:
`R9.2 - restaura Stage1 R5 estável e restringe LLM à evidência`
