NAME = "pim_import_testing"
VERSION = "0.1.0"
DESCRIPTION = "Supplier file import: upload via API or UI, imported/rejected counts, error handling, product count."


def api_import(t, p):
    t.http("POST", "/api/reset")
    r = t.http("POST", "/api/import", file=p["file"])
    t.log("import response", r)
    t.goto("/products")
    return {"imported": r["imported"], "rejected": r["rejected"],
            "error_codes": sorted({e["code"] for e in r["errors"]})}


def ui_import_error(t, p):
    t.http("POST", "/api/reset")
    t.goto("/")
    t.upload("#file", p["file"])
    t.page.click("#upload")
    t.page.wait_for_selector("#summary", timeout=10000)
    codes = sorted({el.get_attribute("data-code") for el in t.page.query_selector_all("#errors li.error")})
    visible = bool(codes) and t.page.is_visible("#errors li.error")
    t.log("ui errors", {"codes": codes, "visible": visible})
    return {"error_visible": visible, "error_codes": codes}


def ui_import_and_count(t, p):
    t.http("POST", "/api/reset")
    t.goto("/")
    t.upload("#file", p["file"])
    t.page.click("#upload")
    t.page.wait_for_selector("#summary", timeout=10000)
    t.goto("/products")
    n = int(t.page.get_attribute("#product-count", "data-count"))
    t.log("displayed count", n)
    return {"displayed_count": n}


ACTIONS = {"api_import": ("api", api_import), "ui_import_error": ("browser", ui_import_error),
           "ui_import_and_count": ("browser", ui_import_and_count)}

# Allowlisted parameters per action (the model may only fill these, from these value domains).
PARAMS = {"api_import": {"file": {"kind": "fixture_file"}}, "ui_import_error": {"file": {"kind": "fixture_file"}},
          "ui_import_and_count": {"file": {"kind": "fixture_file"}}}
