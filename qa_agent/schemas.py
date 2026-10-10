AC_SCHEMA = {
    "type": "object", "required": ["acceptance_criteria"], "additionalProperties": False,
    "properties": {
        "acceptance_criteria": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "text", "source_quote"], "additionalProperties": False,
            "properties": {"id": {"type": "string", "pattern": "^AC[0-9]+$"}, "text": {"type": "string", "minLength": 3},
                           "source_quote": {"type": "string", "minLength": 3}}}},
        "gaps": {"type": "array", "items": {"type": "string"}}}}

PLAN_SCHEMA = {
    "type": "object", "required": ["test_cases"], "additionalProperties": False,
    "properties": {"test_cases": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "title", "type", "skill", "action", "params", "ac_refs", "oracle_ref"],
        "properties": {
            "id": {"type": "string", "pattern": "^TC-[0-9]+$"}, "title": {"type": "string"},
            "type": {"enum": ["api", "browser"]}, "skill": {"type": "string"}, "action": {"type": "string"},
            "params": {"type": "object", "additionalProperties": {"type": "string"}},
            "ac_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "oracle_ref": {"type": "string"}}}}}}
