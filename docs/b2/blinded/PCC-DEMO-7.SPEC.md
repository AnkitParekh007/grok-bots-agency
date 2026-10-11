# Held-out story spec: PCC-DEMO-7 (length normalisation)

This spec was written **before** the story, oracles and data file, and without opening `fixtures/replay/*` or any stub answer.

- **Author:** Grok Bot, the same agent that wrote the stub recordings for PCC-DEMO-2…6. The authors are therefore **not independent**. A human-authored blinded story or reviewer spot checks (see `../INDEPENDENT_REVIEW_TEMPLATE.md`) are still needed for real independence.
- **No stub recording exists for PCC-DEMO-7, by design.** It is evaluated only with a live provider.
- **Gold is sealed:** the gold annotation is kept outside the repository. Only its sha256 is committed, in `fixtures/eval/eval_set.json` → `sealed_gold`. The harness verifies the hash before scoring.

## Business intent
Supplier length values arrive in inches, centimetres or (invalid) metres, and must be stored in millimetres.

## Requirements to encode as acceptance criteria
1. Inch lengths are converted to millimetres at 25.4 mm per inch.
2. Centimetre lengths are converted to millimetres.
3. Rows with length unit `m` are rejected with `UNIT_UNKNOWN` (the dictionary has no metres).
4. The product list page displays the number of imported products.
5. Converted lengths use the viewer's locale-specific decimal separator. This is deliberately untestable: there is no locale skill or oracle.

## Data
`supplier_lengths.csv` has three rows:
- `SKU-7001`: 12 in, expected 304.8 mm.
- `SKU-7002`: 150 cm, expected 1500 mm.
- `SKU-7003`: 3 m, expected to be rejected.

Expected import result: 2 imported, 1 rejected; the list page shows 2.

## Seeded defects expected to be caught
- `inch_factor`: SKU-7001 becomes 30.48 mm.
- `ui_count_off`: the list page shows 1.
