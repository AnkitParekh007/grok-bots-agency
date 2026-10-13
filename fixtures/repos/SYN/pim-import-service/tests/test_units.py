from src.units import to_mm


def test_inch():
    assert to_mm(1, "in") == 25.4
