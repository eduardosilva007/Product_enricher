# Validation Report — R9.5 HF1

- Rules.validate(): 0 errors
- Synthetic: 58/58
- Identity: 16/16
- Real explicit: 22/22

| Modelo | Search Model | State | Type | Power | Category | Family |
|---|---|---|---|---|---|---|
| DJF02 30 220V | DJF02 30 | RESOLVED_RULE | RECIPROCATING SAW | CORDED | OTHERS | RECIPROCATING SAW |
| DJG02 355 220V | DJG02 355 | RESOLVED_RULE | CUT-OFF SAW | UNKNOWN | OTHERS | CUT-OFF SAW |
| DJG09 355 220V | DJG09 355 | RESOLVED_RULE | CUT-OFF SAW | UNKNOWN | OTHERS | CUT-OFF SAW |
| DJX10 255 220V | DJX10 255 | RESOLVED_RULE | MITER SAW | CORDED | OTHERS | MITER SAW |
| DMD320 220V | DMD320 | RESOLVED_RULE | OSCILLATING MULTI-TOOL | CORDED | OTHERS | OSCILLATING MULTI-TOOL |
| DMQ65K 220V | DMQ65K | RESOLVED_RULE | JIGSAW | UNKNOWN | OTHERS | JIGSAW |
| DPL6 220V | DPL6 | RESOLVED_RULE | SCREWDRIVER | UNKNOWN | OTHERS | POWER TOOLS |
| DQF32 | DQF32 | RESOLVED_RULE | BLOWER | CORDED | CORDED PRODUCT | CORDED BLOWER |

## Safety checks

- Descriptive title without target SKU in TITLE/URL/BODY remains UNRESOLVED.
- DQF32 with only `680W` remains power-ambiguous; explicit `220V`/mains evidence is required for CORDED.
- Legacy RAW cache without `_collection` is PENDING_CURATION at runtime.
- `GSA 18V 32` remains unchanged; terminal-spec stripping does not remove internal voltage tokens.
