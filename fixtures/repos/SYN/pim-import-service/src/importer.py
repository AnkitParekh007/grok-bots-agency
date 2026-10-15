"""SYNTHETIC: supplier CSV importer. Rows without a SKU are rejected with SKU_REQUIRED."""
ATTRIBUTE_ALIASES = {"colour": "color"}


def map_row(row: dict) -> dict:
    if not row.get("sku"):
        raise ValueError("SKU_REQUIRED")
    return {ATTRIBUTE_ALIASES.get(k, k): v for k, v in row.items()}
