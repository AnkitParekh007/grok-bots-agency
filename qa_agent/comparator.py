"""Deterministic actual-vs-expected comparison. Expected values come only from the oracle manifest."""


def _eq(exp, act, tol):
    if isinstance(exp, bool) or isinstance(act, bool):
        return exp is act if isinstance(exp, bool) and isinstance(act, bool) else False
    if isinstance(exp, (int, float)) and isinstance(act, (int, float)):
        return abs(exp - act) <= tol
    if isinstance(exp, list) and isinstance(act, list):
        return sorted(map(str, exp)) == sorted(map(str, act))
    return exp == act


def compare(expected: dict, actual: dict, tolerance: float = 0.0) -> dict:
    diffs = []
    for field, exp in sorted(expected.items()):
        if field not in actual:
            diffs.append({"field": field, "expected": exp, "actual": "<missing>"})
        elif not _eq(exp, actual[field], tolerance):
            diffs.append({"field": field, "expected": exp, "actual": actual[field]})
    return {"passed": not diffs, "diffs": diffs, "checked_fields": sorted(expected)}
