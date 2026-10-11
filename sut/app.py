"""Synthetic PIM-like application under test. NOT a product. Loopback only.

Seeded defects are toggled with env SUT_DEFECTS (comma list or 'all'):
  inch_factor        - inch->mm uses 2.54 instead of 25.4
  drop_color         - attribute mapping drops the colour/color attribute
  no_sku_validation  - rows without SKU are imported instead of rejected
  ui_count_off       - product list page shows count - 1
  etim_alias_ignored - legacy supplier column "etim" is not mapped to etim_class (B2 eval, PCC-DEMO-5)
  name_not_required  - rows without a product name are imported instead of rejected (B2 eval, PCC-DEMO-6)
Fault injection (never part of 'all'; must be named explicitly):
  slow_api           - GET /api/products/{sku} sleeps 3 s (exercises agent timeouts -> ERROR, never PASS)
"""
import csv, html, io, os, time
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse

ALL_DEFECTS = {"inch_factor", "drop_color", "no_sku_validation", "ui_count_off", "etim_alias_ignored", "name_not_required"}
FAULTS = {"slow_api"}


def active_defects() -> set:
    raw = os.environ.get("SUT_DEFECTS", "").strip()
    if raw == "all":
        return set(ALL_DEFECTS)
    return {d for d in (x.strip() for x in raw.split(",")) if d in ALL_DEFECTS | FAULTS}


ALIASES = {"colour": "color", "color": "color", "material": "material",
           "etim_class": "etim_class", "etim": "etim_class", "ip_rating": "ip_rating"}
LENGTH = {"mm": 1.0, "cm": 10.0, "in": 25.4}
WEIGHT = {"kg": 1.0, "g": 0.001, "lb": 0.45359237}

app = FastAPI(title="Synthetic PIM (SUT)")
PRODUCTS: dict = {}


def process_csv(text: str) -> dict:
    defects = active_defects()
    imported, errors = 0, []
    for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        sku = (row.get("sku") or "").strip()
        if not sku and "no_sku_validation" not in defects:
            errors.append({"line": line, "code": "SKU_REQUIRED", "message": "SKU is required"})
            continue
        if not (row.get("name") or "").strip() and "name_not_required" not in defects:
            errors.append({"line": line, "code": "NAME_REQUIRED", "message": "Product name is required"})
            continue
        lu, wu = row.get("length_unit", "").strip(), row.get("weight_unit", "").strip()
        if lu not in LENGTH or wu not in WEIGHT:
            errors.append({"line": line, "code": "UNIT_UNKNOWN", "message": f"Unknown unit {lu}/{wu}"})
            continue
        lf = 2.54 if (lu == "in" and "inch_factor" in defects) else LENGTH[lu]
        attrs = {}
        for col, val in row.items():
            target = ALIASES.get(col)
            if not target:
                continue
            if target == "color" and "drop_color" in defects:
                continue
            if col == "etim" and "etim_alias_ignored" in defects:
                continue
            attrs[target] = val.strip()
        key = sku or f"(blank-line-{line})"
        PRODUCTS[key] = {
            "sku": key, "name": row.get("name", "").strip(),
            "length_mm": round(float(row["length"]) * lf, 3),
            "weight_kg": round(float(row["weight"]) * WEIGHT[wu], 3),
            "attributes": attrs,
        }
        imported += 1
    return {"imported": imported, "rejected": len(errors), "errors": errors}


@app.get("/api/health")
def health():
    return {"ok": True, "defects": sorted(active_defects())}


@app.post("/api/reset")
def reset():
    PRODUCTS.clear()
    return {"ok": True}


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)):
    return process_csv((await file.read()).decode("utf-8"))


@app.get("/api/products")
def api_products():
    return {"count": len(PRODUCTS), "products": list(PRODUCTS.values())}


@app.get("/api/products/{sku}")
def api_product(sku: str):
    if "slow_api" in active_defects():
        time.sleep(3)
    if sku not in PRODUCTS:
        raise HTTPException(404, "not found")
    return PRODUCTS[sku]


PAGE = """<!doctype html><html><head><title>Synthetic PIM</title>
<style>body{{font-family:sans-serif;margin:2em}} .err{{color:#b00}} table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:4px 8px}}</style>
</head><body><div style="background:#fe0;padding:4px">SYNTHETIC TEST APPLICATION - no real data</div>{body}</body></html>"""


@app.get("/", response_class=HTMLResponse)
def upload_page():
    return PAGE.format(body="""
<h1>Supplier import</h1>
<form id="f"><input type="file" id="file" name="file" accept=".csv">
<button id="upload" type="submit">Upload</button></form>
<div id="result"></div><ul id="errors" class="err"></ul>
<p><a href="/products">Product list</a></p>
<script>
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(); fd.append('file', document.getElementById('file').files[0]);
  const r = await (await fetch('/api/import', {method:'POST', body: fd})).json();
  document.getElementById('result').innerHTML = `<p id="summary" data-imported="${r.imported}" data-rejected="${r.rejected}">Imported ${r.imported}, rejected ${r.rejected}</p>`;
  document.getElementById('errors').innerHTML = r.errors.map(x => `<li class="error" data-code="${x.code}">Line ${x.line}: ${x.code} - ${x.message}</li>`).join('');
};
</script>""")


@app.get("/products", response_class=HTMLResponse)
def products_page():
    n = len(PRODUCTS) - (1 if "ui_count_off" in active_defects() and PRODUCTS else 0)
    rows = "".join(
        f"<tr><td>{html.escape(p['sku'])}</td><td>{html.escape(p['name'])}</td><td>{p['length_mm']}</td>"
        f"<td>{p['weight_kg']}</td><td>{html.escape(str(p['attributes']))}</td></tr>" for p in PRODUCTS.values())
    return PAGE.format(body=f"""<h1>Products</h1><p id="product-count" data-count="{n}">{n} products</p>
<table><tr><th>SKU</th><th>Name</th><th>Length (mm)</th><th>Weight (kg)</th><th>Attributes</th></tr>{rows}</table>""")


@app.get("/products/{sku}", response_class=HTMLResponse)
def product_page(sku: str):
    p = PRODUCTS.get(sku)
    body = f"<h1>{html.escape(sku)}</h1><pre id='product'>{html.escape(str(p))}</pre>" if p else "<h1>Not found</h1>"
    return PAGE.format(body=body)
