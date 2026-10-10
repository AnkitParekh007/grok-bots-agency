from qa_agent.comparator import compare


def test_match_with_tolerance():
    assert compare({"length_mm": 254.0}, {"length_mm": 254.0004}, 0.001)["passed"]


def test_mismatch_reports_field():
    r = compare({"length_mm": 254.0, "sku": "A"}, {"length_mm": 25.4, "sku": "A"}, 0.001)
    assert not r["passed"] and r["diffs"] == [{"field": "length_mm", "expected": 254.0, "actual": 25.4}]


def test_missing_field_fails():
    r = compare({"attributes.color": "Grey"}, {"sku": "X"})
    assert not r["passed"] and r["diffs"][0]["actual"] == "<missing>"


def test_bool_not_equal_int_and_lists_order_insensitive():
    assert not compare({"error_visible": True}, {"error_visible": 1})["passed"]
    assert compare({"codes": ["A", "B"]}, {"codes": ["B", "A"]})["passed"]
    assert not compare({"codes": ["A"]}, {"codes": []})["passed"]


def test_deterministic():
    a = compare({"x": 1, "y": [1, 2]}, {"x": 2, "y": [2]})
    assert all(compare({"x": 1, "y": [1, 2]}, {"x": 2, "y": [2]}) == a for _ in range(20))
