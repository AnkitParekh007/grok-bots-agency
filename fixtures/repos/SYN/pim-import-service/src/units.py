"""SYNTHETIC: unit normalisation used by the synthetic import service."""
LENGTH_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4}
WEIGHT_TO_KG = {"kg": 1.0, "g": 0.001, "lb": 0.45359237}


def to_mm(value: float, unit: str) -> float:
    return round(value * LENGTH_TO_MM[unit], 3)
