"""
Apply remaining MAP-catalog price/tag corrections to Detective Hawk Games' Shopify store.

Uses the direct Shopify Admin GraphQL API (same pattern as dhg-asmodee-reconcile-app).
Requires a Shopify Admin API access token with write_products scope.

Usage:
    export SHOPIFY_SHOP="your-shop.myshopify.com"
    export SHOPIFY_ADMIN_TOKEN="shpat_xxxxxxxx"
    pip install requests --break-system-packages
    python3 apply_map_corrections.py

What this does, in order:
  1. price_updates.json  (53 rows) -> sets variant price (and compareAtPrice where provided)
     to match the real MAP catalog value for that SKU.
  2. tags_add.json       (34 rows) -> adds missing 'speakeasy'/'mapp rules' tags to products
     where MSRP == MAP (Asmodee doesn't allow any advertised discount, so the cart-discount
     workaround tags are needed).
  3. tags_remove_remaining.json (140 rows, 5 already done manually in-session) -> removes
     'speakeasy'/'mapp rules' tags from products where MSRP != MAP (a real discount is
     already allowed, so the cart-discount workaround tags shouldn't be present).

Each row is applied individually with error handling; failures are logged and skipped
rather than halting the run. A summary is printed at the end and a log file is written.
"""
import json
import os
import sys
import time
import requests

SHOP = os.environ.get("SHOPIFY_SHOP")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN")
API_VERSION = "2025-01"

if not SHOP or not TOKEN:
    print("ERROR: Set SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN environment variables first.")
    sys.exit(1)

URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": TOKEN,
}

PRICE_MUTATION = """
mutation call($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id price compareAtPrice }
    userErrors { field message }
  }
}
"""

TAGS_ADD_MUTATION = """
mutation r($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
}
"""

TAGS_REMOVE_MUTATION = """
mutation r($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
}
"""


def run_mutation(query, variables, retries=3):
    for attempt in range(retries):
        resp = requests.post(URL, headers=HEADERS, json={"query": query, "variables": variables}, timeout=30)
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        data = resp.json()
        if "errors" in data:
            return False, data["errors"]
        # Check for userErrors nested in the response
        for key in data.get("data", {}):
            payload = data["data"][key]
            if isinstance(payload, dict) and payload.get("userErrors"):
                return False, payload["userErrors"]
        return True, data
    return False, "Rate limited after retries"


def main():
    log = []
    base = os.path.dirname(os.path.abspath(__file__))

    # --- 1. Price updates ---
    price_path = os.path.join(base, "price_updates.json")
    with open(price_path) as f:
        price_updates = json.load(f)
    print(f"Applying {len(price_updates)} price updates...")
    for r in price_updates:
        variant = {"id": r["variantId"], "price": r["price"]}
        if r.get("compareAtPrice"):
            variant["compareAtPrice"] = r["compareAtPrice"]
        ok, result = run_mutation(PRICE_MUTATION, {"productId": r["productId"], "variants": [variant]})
        status = "OK" if ok else f"FAIL: {result}"
        log.append(f"[PRICE] {r['sku']} -> {r['price']}: {status}")
        print(log[-1])
        time.sleep(0.5)

    # --- 2. Tags add ---
    tags_add_path = os.path.join(base, "tags_add.json")
    with open(tags_add_path) as f:
        tags_add = json.load(f)
    print(f"\nApplying {len(tags_add)} tag additions...")
    for r in tags_add:
        ok, result = run_mutation(TAGS_ADD_MUTATION, {"id": r["productId"], "tags": r["tags"]})
        status = "OK" if ok else f"FAIL: {result}"
        log.append(f"[TAG ADD] {r['sku']} +{r['tags']}: {status}")
        print(log[-1])
        time.sleep(0.5)

    # --- 3. Tags remove (remaining, excludes 5 done manually in-session) ---
    tags_remove_path = os.path.join(base, "tags_remove_remaining.json")
    with open(tags_remove_path) as f:
        tags_remove = json.load(f)
    print(f"\nApplying {len(tags_remove)} tag removals...")
    for r in tags_remove:
        ok, result = run_mutation(TAGS_REMOVE_MUTATION, {"id": r["productId"], "tags": r["tags"]})
        status = "OK" if ok else f"FAIL: {result}"
        log.append(f"[TAG REMOVE] {r['sku']} -{r['tags']}: {status}")
        print(log[-1])
        time.sleep(0.5)

    log_path = os.path.join(base, "map_correction_log.txt")
    with open(log_path, "w") as f:
        f.write("\n".join(log))

    fails = [l for l in log if "FAIL" in l]
    print(f"\nDone. {len(log)} operations, {len(fails)} failures.")
    print(f"Full log written to {log_path}")
    if fails:
        print("\nFailures:")
        for l in fails:
            print(" ", l)


if __name__ == "__main__":
    main()
