"""Named planning contracts (JSON Schema, consistent with the existing jsonschema approach).

The original required fields remain compatible with B1. Selected contextual sources
add a mandatory context_review when present; these names define the wire contract:

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

ContextCitation = {
    "type": "object", "additionalProperties": False,
    "required": ["quote", "line_start", "line_end", "test_refs"],
    "properties": {"quote": {"type": "string", "minLength": 3, "maxLength": 2000}, "line_start": {"type": "integer", "minimum": 1, "maximum": 100001},
                   "line_end": {"type": "integer", "minimum": 1, "maximum": 100001},
                   "test_refs": {"type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
                                 "items": {"type": "string", "pattern": "^TC-[0-9]+$"}}}}

ContextReview = {
    "type": "object", "additionalProperties": False,
    "required": ["source_id", "source_sha256", "decision", "reason", "citations"],
    "properties": {"source_id": {"type": "string", "minLength": 1, "maxLength": 500},
                   "source_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                   "decision": {"enum": ["used", "not_used"]}, "reason": STR,
                   "citations": {"type": "array", "maxItems": 5, "items": ContextCitation}}}

TestPlan = {
    "type": "object", "additionalProperties": False,
    "required": ["understanding", "acceptance_criteria", "behaviors", "selected_skills", "test_cases", "coverage_gaps"],
    "properties": {
        "understanding": Understanding,
        "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": 50, "items": AcceptanceCriterion},
        "behaviors": {"type": "array", "maxItems": 100, "items": Requirement},
        "selected_skills": SkillSelection,
        "test_cases": {"type": "array", "maxItems": 50, "items": TestScenario},
        "context_review": {"type": "array", "maxItems": 10, "items": ContextReview},
        "coverage_gaps": {"type": "array", "items": CoverageGap}}}

PlanValidationResult = {
    "type": "object", "additionalProperties": False, "required": ["ok", "category", "errors", "catalog_version"],
    "properties": {"ok": {"type": "boolean"}, "category": {"enum": ["ok", "format", "policy"]},
                   "errors": {"type": "array", "items": {"type": "string"}}, "catalog_version": {"type": "string"},
                   "plan_hash": {"type": ["string", "null"]}}}
