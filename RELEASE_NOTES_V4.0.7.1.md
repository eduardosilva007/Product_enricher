# PIPELINE v4.0.7.1-identity-rescue-fix

## Motivo da revisão

O primeiro piloto real da v4.0.7 processou 18 novas buscas:

- 11 READY_FOR_CLASSIFICATION
- 7 NO_TARGET_EVIDENCE
- 176 fontes auditadas
- 91 REJECT_WEAK_IDENTITY

A auditoria do relatório mostrou que parte dos NO_TARGET era falso negativo de identidade/isolamento, apesar de haver informação clara na internet.

Casos usados como regressão:
- ANTHBOT GENIE 1000 EU
- ANTHBOT GENIE 3000 EU
- MILWAUKEE 2446 21XC M12
- BRIGGS & STRATTON 44S9770046G1AF0001
- BRIGGS & STRATTON 08P6020022H5A0096
- BRIGGS&STRATTON 40U7770010

MILWAUKEE L64BF241012109 permanece propositalmente sem regra de recuperação: o piloto não apresentou prova de que esse identificador seja um modelo comercial. A v4.0.7.1 não tenta adivinhar códigos seriais/internos.

## Correções

### 1. Normalização brand-specific sem fuzzy

ANTHBOT:
- EU é tratado como sufixo operacional apenas para busca/identidade.
- GENIE 1000 EU -> Search Model GENIE 1000.
- GENIE1000, GENIE-1000 e GENIE 1000 continuam estruturalmente equivalentes.
- GENIE 3000 não pode casar com GENIE 1000.

MILWAUKEE:
- M12/M18 podem ser removidos quando aparecem como token operacional final configurado.
- 2446 21XC M12 -> Search Model 2446 21XC.
- Não existe edit-distance/fuzzy.

BRIGGS & STRATTON:
- códigos internos completos podem ser reescritos, somente para esta marca, para o padrão publicado Model-Type-Trim.
- 44S9770046G1AF0001 -> 44S977 0046 G1
- 08P6020022H5A0096 -> 08P602 0022 H5
- 6134774205J1EB0001 -> 613477 4205 J1
- 40U7770010 -> 40U777 0010

O rewrite exige fullmatch do formato configurado.

### 2. Correção do falso cross-SKU da Briggs

40U7770010 era reconhecido no TITLE, mas 40U777-0010-G1 era depois tratado como "outro SKU" pelo Target Isolation.

Agora, exclusivamente para Briggs & Stratton:
- Model+Type + Trim letra/dígito é reconhecido como o mesmo target;
- somente quando o prefixo de 10 caracteres é idêntico;
- 40U777-0010-G1 não torna 44U777-0010-G1 equivalente.

### 3. Fila de classificação restrita ao arquivo de entrada

Com `restrict_classify_to_input=true`, a opção CLASSIFICAÇÃO considera apenas business_keys presentes no arquivo de entrada atual.

Isso impede que um piloto de 20 itens seja preenchido por produtos históricos do cache de milhares de registros.

Se o arquivo de entrada não existir ou não tiver Marca+Modelo válidos, a classificação falha de forma fechada.

### 4. SAFE AUXILIARY RESCUE

NO_TARGET_EVIDENCE sem curated_results pode entrar na fila somente quando o Evidence Cache possui AUXILIARY_REVIEW_ONLY com identidade STRONG/PROBABLE e sem:
- wrong-model;
- compatibility-only;
- related-only.

Esse material continua não sendo PRIMARY.

### 5. Rescue-only nunca AUTO

Quando um produto entrou apenas via TITLE_ONLY_RESCUE/AUXILIARY:
- regra forte não fecha AUTO diretamente;
- o caso segue para Clef;
- mesmo uma resposta que passaria no AUTO gate é rebaixada para CLASSIFIED_CLEF;
- exige double-check.

Isso evita recuperar falsos negativos ao custo de criar falsos positivos automáticos.

### 6. REVIEW_CONTEXT_UNVERIFIED separado

O Evidence Bundle mantém:
- llm_context: PRIMARY + AUXILIARY seguros;
- review_context_unverified: fontes REJECTED apenas por weak identity/isolamento.

Wrong-model, compatibility-only e related-only ficam fora do review context.

O review_context_unverified é explicitamente rotulado como não confirmado e nunca alimenta AUTO.

### 7. Telemetria e versionamento

Pipeline:
- 4.0.7.1-identity-rescue-fix

Collection:
- R9.6p7.2.1

Novos relatórios:
- Coleta_Search_R9_6p7_2_1.xlsx
- Pre_Coleta_R9_6p7_2_1.xlsx
- Curadoria_Cache_R9_6p7_2_1.xlsx
- Evidence_Bundle_v4_0_7_1.jsonl

Os labels de console deixam de exibir R9.6/R9.6p6 antigo.

## Contratos preservados

- máximo 1 Search request por produto;
- sem retry automático;
- sem segunda query;
- sem provider failover;
- sem Tavily Extract;
- sem Advanced;
- sem alterações em Clef/BGE/reranker/thresholds;
- sem alterações em regras_negocio.json;
- Revisão B.I=OK continua HARD LOCK;
- AUTO continua exigindo evidência forte e, no rescue-only, AUTO é explicitamente proibido.

## Regressões adicionadas

`test_v4071_identity_rescue.py` cobre:
- ANTHBOT EU;
- GENIE1000 estrutural;
- bloqueio GENIE1000 x GENIE3000;
- Milwaukee M12;
- Briggs Model-Type-Trim;
- bloqueio de Type incorreto Briggs;
- correção self-SKU 40U777-0010-G1;
- Target Isolation do 40U777;
- safe auxiliary rescue;
- bloqueio compatibility-only;
- cohort restrito ao input;
- review context sem fontes perigosas.

## Próximo piloto

Após merge e validação local:
1. recolher os mesmos casos falsamente negativos em uma base piloto;
2. verificar se ANTHBOT/Milwaukee/Briggs migram de NO_TARGET para READY/PARTIAL;
3. classificar exatamente o mesmo cohort;
4. auditar qualquer classificação rescue como double-check;
5. só então ampliar para 50/100 e depois 7.700.
