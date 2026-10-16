"""Named planning contracts (JSON Schema, consistent with the existing jsonschema approach).

The wire format the model returns is unchanged from B1; these names make each part explicit and reusable:

  Requirement          -> one entry of `behaviors` (a testable/untestable behaviour derived from ACs)
  AcceptanceCriterion  -> one entry of `acceptance_criteria` (verbatim-quote traceable)
  TestStep             -> the executable step embedded in a scenario: skill + action + params (single-step in B1)
  TestScenario         -> one entry of `test_cases` (TestStep + id/title/type/category/refs/oracle)
  OracleReference      -> `oracle_ref` (id of an approved oracle in the reviewer manifest)
  SkillSelection       -> `selected_skills`
  CoverageGap          -> one entry of `coverage_gaps`
  TestPlan             -> the whole model output (PLAN_STORY_SCHEMA)
  PlanValidationResult -> result of planning.evaluate_plan() (not produced by the model)
"""
CATEGORIES = ["positive", "negative", "edge"]
GAP_KINDS = ["no_skill_action", "no_approved_oracle", "ambiguous_requirement", "out_of_scope", "untrusted_instruction_ignored"]
STR = {"type": "string", "minLength": 1, "maxLength": 2000}

AcceptanceCriterion = {
    "type": "object", "additionalProperties": False, "required": ["id", "text", "source_quote"],
    "properties": {"id": {"type": "string", "pattern": "^AC[0-9]+$"}, "text": STR,
                   "source_quote": {"type": "string", "minLength": 3, "maxLength": 2000},
                   "source_id": {"type": "string", "minLength": 1, "maxLength": 500},
                   "source_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}

Requirement = {
    "type": "object", "additionalProperties": False, "required": ["id", "ac_refs", "description", "testable"],
    "properties": {"id": {"type": "string", "pattern": "^B-[0-9]+$"}, "ac_refs": {"type": "array", "items": {"type": "string"}},
                   "description": STR, "testable": {"type": "boolean"}}}

OracleReference = {"type": "string", "pattern": "^[A-Z0-9][A-Z0-9-]{1,60}$"}

TestStep = {  # fields of the executable step (embedded flat in TestScenario for wire compatibility)
    "skill": {"type": "string", "minLength": 1}, "action": {"type": "string", "minLength": 1},
    "params": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200}}}

TestScenario = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "title", "type", "category", "behavior_ref", "skill", "action", "params", "ac_refs", "oracle_ref"],
    "properties": {
        "id": {"type": "string", "pattern": "^TC-[0-9]+$"}, "title": STR,
        "type": {"enum": ["api", "browser"]}, "category": {"enum": CATEGORIES},
        "behavior_ref": {"type": "string"}, **TestStep,
        "ac_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "oracle_ref": OracleReference}}

SkillSelection = {"type": "array", "items": {"type": "string"}}

CoverageGap = {
    "type": "object", "additionalProperties": False, "required": ["ac_refs", "kind", "reason"],
    "properties": {"ac_refs": {"type": "array", "items": {"type": "string"}}, "kind": {"enum": GAP_KINDS}, "reason": STR}}

Understanding = {
    "type": "object", "additionalProperties": False, "required": ["summary", "domain", "assumptions"],
    "properties": {"summary": STR, "domain": STR, "assumptions": {"type": "array", "items": STR}}}

TestPlan = {
    "type": "object", "additionalProperties": False,
    "required": ["understanding", "acceptance_criteria", "behaviors", "selected_skills", "test_cases", "coverage_gaps"],
    "properties": {
        "understanding": Understanding,
        "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": 50, "items": AcceptanceCriterion},
        "behaviors": {"type": "array", "maxItems": 100, "items": Requirement},
        "selected_skills": SkillSelection,
        "test_cases": {"type": "array", "maxItems": 50, "items": TestScenario},
        "coverage_gaps": {"type": "array", "items": CoverageGap}}}

PlanValidationResult = {
    "type": "object", "additionalProperties": False, "required": ["ok", "category", "errors", "catalog_version"],
    "properties": {"ok": {"type": "boolean"}, "category": {"enum": ["ok", "format", "policy"]},
                   "errors": {"type": "array", "items": {"type": "string"}}, "catalog_version": {"type": "string"},
                   "plan_hash": {"type": ["string", "null"]}}}
