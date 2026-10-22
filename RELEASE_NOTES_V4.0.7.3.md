# PIPELINE v4.0.7.3-structural-sku-core

## Root cause

The R9.6p7.2.2 pilot for MILWAUKEE 2446 21XC M12 returned 10 Search results, including explicit 2446-21XC references from Milwaukee, Home Depot, Southern Pipe, YouTube and eBay, but 9/10 sources were rejected as WEAK.

The failure was not Search quality. It was the identity model: the internal Model field "2446 21XC M12" was treated as one rigid ordered string. Published sources commonly use:
- 2446-21XC
- M12 2446-21XC
- URL path .../2446-21xc

This caused technically easy products to become NO_TARGET_EVIDENCE.

## Architectural fix: Structural SKU Core

Identity now has two layers:

1. ORIGINAL
   - the complete source Model remains untouched and is always the first identity variant.

2. STRUCTURAL SKU CORE
   - only edge decorators may be removed;
   - no fuzzy matching;
   - no edit distance;
   - no page-wide bag-of-tokens;
   - the remaining core must still be sufficiently specific.

Examples:

- 2446 21XC M12
  - EXACT: 244621xcm12
  - CORE: 244621xc

- GENIE 1000 EU
  - EXACT: genie1000eu
  - CORE: genie1000

The same variants are consumed by:
- Source Identity
- Target Spans
- Target Isolation
- Other-SKU detection
- Title Rescue
- Evidence Cache

This removes the previous dependency on brand-specific M12/EU patches.

## Edge decorators

The versioned profile currently recognizes only conservative edge forms:
- M12 / M18
- EU / US / UK / AU
- voltage tokens
- Ah tokens
- Hz tokens
- W tokens
- existing configured regional suffixes

An edge token is removed only when the remaining model:
- has at least 2 tokens;
- has at least 6 compact characters;
- contains digits;
- contains either a mixed alpha+numeric token or a long numeric token plus alphabetic context.

Therefore a generic model like ABC 123 M12 does NOT produce ABC123 as a CORE.

## Wrong variant protection

For target:
- MILWAUKEE 2446 21XC M12

Accepted identity:
- Milwaukee M12 2446-21XC Grease Gun Kit
- URL /2446-21xc

Not accepted as target identity:
- Milwaukee 2446-20 M12 Grease Gun

Brand-only evidence may remain CONTEXTUAL but never becomes PROBABLE/STRONG.

## Search and identity are decoupled

Search query formatting no longer defines what counts as the SKU.

A query may retain useful platform/region tokens while Identity independently recognizes the published SKU CORE.

This prevents future collection failures caused by different token order or omitted platform labels.

## Audit improvements

Collection reports now persist Identity Variants alongside Search Model so every pilot shows exactly which deterministic variants were available.

## Runtime contract

Before any Search credit is spent, the script validates:
- expected structural CORE variants;
- real positive source identity examples;
- wrong-variant negative examples;
- Briggs structural rewrites;
- structural profile configuration;
- fuzzy_matching=false.

Any drift blocks execution before Search.

## Preserved invariants

- max 1 Search request/product
- no retry
- no second query
- no provider failover
- no Extract/Advanced
- no fuzzy identity
- no taxonomy changes
- no Clef/BGE/reranker threshold changes
- rescue-only cannot AUTO
- Revisão B.I=OK remains HARD LOCK
- original Modelo/business_key are never replaced by CORE

## Version

- Pipeline: 4.0.7.3-structural-sku-core
- Collection: R9.6p7.2.3
- Identity profile: 2026-10-22-structural-sku-core-v3
- Rules unchanged: 2026-10-13-v3.0.13-manual-taxonomy-gold-2
