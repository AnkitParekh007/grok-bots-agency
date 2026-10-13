# Contamination and independence: what the B2 evaluation can and cannot show

## Checks in place
| Check | What it catches | What it does NOT catch |
|---|---|---|
| `contaminated_plans` (**hard gate**, live only): canonical JSON of the live plan == the recorded stub answer | Verbatim replay of a recording, e.g. a misconfigured stub endpoint or a cached answer | Any paraphrase, re-ordering or partial copy |
| `near_copy_similarity` (**heuristic, not gated**): Jaccard of normalized tokens of scenario titles, actions and params against the recorded answer; flagged at ≥ 0.80 | Close paraphrases of the recording | Semantic overlap expressed differently. It also has false positives, because good plans for the same story share vocabulary (SKUs, file names, action names) |
| Loopback/stub refusal in `--provider grok` | Accidentally "evaluating" the local stub | n/a |
| Prompt check (`test_prompt_never_contains_recorded_answers`) | Recorded answers leaking into prompts | What the provider saw in pre-training or elsewhere |
| Held-out story PCC-DEMO-7: no stub recording, gold sealed outside the repo (sha256 committed, verified at scoring) | Tuning prompts or validators against the held-out answers inside this repo | Bias of the author, who is the same agent that wrote the other recordings (see below) |

## Limitations
- None of these checks can prove that the model reasoned independently. The stories, catalog and oracles are public in this repository, and the model may have seen similar material.
- PCC-DEMO-2…6 were authored by the same agent as their stub answers. PCC-DEMO-7 was written from a separate spec without consulting the recordings, but **by the same agent**, so it is not author-independent.
- The gold annotations reflect one author's reading of each story and are pending QA-lead review.

## Required mitigation before relying on results
1. At least one **human-authored blinded story**, written by someone who has not seen the repo's recordings, with sealed gold. It can be added under `held_out_stories` with its hash in `sealed_gold`.
2. **Independent reviewer spot checks** of live plans using `INDEPENDENT_REVIEW_TEMPLATE.md`. Suggested coverage: at least one run per story plus every run flagged near-copy.
