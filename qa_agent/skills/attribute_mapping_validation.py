NAME = "attribute_mapping_validation"
VERSION = "0.1.0"
DESCRIPTION = "Per-SKU attribute mapping and unit-of-measure normalisation checks against an approved dictionary/manifest."


def api_product_after_import(t, p):
    t.http("POST", "/api/reset")
    t.http("POST", "/api/import", file=p["file"])
    prod = t.http("GET", f"/api/products/{p['sku']}")
    t.log("product", prod)
    t.goto(f"/products/{p['sku']}")
    flat = {"sku": prod.get("sku"), "length_mm": prod.get("length_mm"), "weight_kg": prod.get("weight_kg")}
    for k, v in (prod.get("attributes") or {}).items():
        flat[f"attributes.{k}"] = v
    return flat


ACTIONS = {"api_product_after_import": ("api", api_product_after_import)}

# Allowlisted parameters per action (the model may only fill these, from these value domains).
PARAMS = {"api_product_after_import": {"file": {"kind": "fixture_file"}, "sku": {"kind": "sku"}}}
