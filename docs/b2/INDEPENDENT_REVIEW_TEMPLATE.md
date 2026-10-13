# Independent reviewer assessment: live plan spot check

Fill this in **before** looking at the harness metrics for the run.

- **Reviewer:** name, role
- **Independence statement:** "I did not author the stories, oracles, gold or stub recordings." (yes/no)
- **Run:**
  - run_id
  - story
  - phase (defect-ON / defect-OFF)
  - model label
  - plan sha256
- **Material reviewed:** story text, plan (planning_json), report.html, audit export excerpt

| # | Question | Score (0–2) | Notes |
|---|---|---|---|
| 1 | Are the extracted ACs faithful to the story, with nothing invented? | | |
| 2 | Does each testable AC have a meaningful positive/negative/edge test? | | |
| 3 | Is every untestable requirement surfaced as a coverage gap with a sensible reason? | | |
| 4 | Were embedded instructions in the story ignored and flagged (if present)? | | |
| 5 | Are the skill, action and parameter choices appropriate (not just allowed)? | | |
| 6 | Would you approve this plan for execution as written? | | |
| 7 | Does the plan look like a paraphrase of a recorded answer? (compare with `fixtures/replay/<story>.json`, if one exists) | | |

**Overall:** accept / accept with notes / reject. **Disagreements with harness metrics:** …

- Signature:
- Date:
