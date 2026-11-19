"""
Wasalt scraper.

Wasalt is a Next.js site: each search-results page embeds up to 32 listings in
<script id="__NEXT_DATA__"> -> props.pageProps.searchResult.properties.
We build search URLs (city x property type x page), read that JSON, normalise
the fields, and drop personal data (owner / agent names, licence numbers).

Fetching: plain HTTP first; if the page comes back without __NEXT_DATA__
(bot protection), it falls back to your own Chrome via CDP, same as DarGlobal:
    Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/dg-chrome-profile"
    python scrape_wasalt.py --cdp

Usage:  python scrape_wasalt.py                 # Riyadh + Jeddah, villas + apartments
        python scrape_wasalt.py --max-pages 4   # 4 pages (~128 listings) per search
Outputs: data/wasalt_listings.json, data/wasalt_docs.jsonl
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = "https://wasalt.sa"
OUT = Path("data")
CITIES = {"Riyadh": 273, "Jeddah": 274}
# propertyTypeData slugs. "villa-townhouse" is confirmed from the site; if "apartment"
# returns mixed types, check the URL after choosing "Apartment" in the site's filter.
PROPERTY_TYPES = ["villa-townhouse", "apartment"]
PAGE_SIZE = 32
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
IMG = "https://imagedelivery.net/1DNKFJPRaeUdy_j8F7HT3w/production/properties/{id}/images/{name}/width=800,quality=70,format=auto"


def arg(name: str, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--") else True
    return default


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, cdp_url: str | None):
        self.http = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
        self.cdp_url = cdp_url
        self._pw = self._page = None

    def _cdp_page(self):
        if self._page is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            browser = self._pw.chromium.connect_over_cdp(self.cdp_url or "http://localhost:9222")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self._page = ctx.new_page()
            print("  (using your Chrome via CDP)")
        return self._page

    def next_data(self, url: str) -> dict | None:
        if not self.cdp_url:
            try:
                r = self.http.get(url)
                node = HTMLParser(r.text).css_first("script#__NEXT_DATA__")
                if r.status_code == 200 and node:
                    return json.loads(node.text())
                print(f"  plain HTTP got no data (status {r.status_code}); switching to Chrome")
                self.cdp_url = "http://localhost:9222"
            except httpx.HTTPError as e:
                print(f"  HTTP error {e}; switching to Chrome")
                self.cdp_url = "http://localhost:9222"
        page = self._cdp_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=25_000)
        except Exception:
            return None
        txt = page.evaluate("() => document.querySelector('script#__NEXT_DATA__').textContent")
        return json.loads(txt)

    def close(self):
        self.http.close()
        if self._page:
            self._page.close()
        if self._pw:
            self._pw.stop()


# ---------------------------------------------------------------- parsing
def attrs_map(attrs: list) -> dict:
    out = {}
    for a in attrs or []:
        key = a.get("key")
        if key == "carpetArea":
            key = "landArea"
        if key and a.get("value") not in (None, ""):
            out[key] = a["value"]
    return out


def to_num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_listing(p: dict, scraped_at: str) -> dict | None:
    info = p.get("propertyInfo")
    if not info:
        return None
    a = attrs_map(p.get("attributes"))
    loc = p.get("location") or {}
    images = (p.get("propertyFiles") or {}).get("images") or []
    return {
        "source": "wasalt",
        "kind": "listing",
        "id": p["id"],
        "url": f"{BASE}/en/property/{info.get('propertyFor', 'sale')}/{info['slug']}",
        "title": info.get("title"),
        "property_type": info.get("propertySubType"),
        "purpose": info.get("propertyFor"),
        "city": info.get("city"),
        "district": info.get("district"),
        "territory": info.get("territory"),
        "address": info.get("address"),
        "price": info.get("salePrice") or info.get("conversionPrice"),
        "currency": info.get("currencyType") or "SAR",
        "bedrooms": a.get("noOfBedrooms"),
        "bathrooms": a.get("noOfBathrooms"),
        "built_up_area_sqm": to_num(a.get("builtUpArea")),
        "land_area_sqm": to_num(a.get("landArea")),
        "age": a.get("completionYear"),
        "facing": info.get("facingType"),
        "furnishing": info.get("furnishingType"),
        "lat": to_num(loc.get("lat")),
        "lon": to_num(loc.get("lon")),
        "is_rega_licensed": p.get("isRegaProp"),
        "published_at": p.get("publishedAt"),
        "updated_at": p.get("updatedAt"),
        "image": IMG.format(id=p["id"], name=images[0]) if images else None,
        "scraped_at": scraped_at,
        # deliberately NOT stored: propertyOwner, leadContactInfo, managedBy (personal data)
    }


def parse_project(p: dict, scraped_at: str) -> dict | None:
    info = p.get("projectInfo")
    if not info:
        return None
    a = attrs_map(p.get("attributes"))
    loc = p.get("location") or {}
    return {
        "source": "wasalt",
        "kind": "project",
        "id": p["id"],
        "url": f"{BASE}/en/project/{info['slug']}",  # project URL pattern: verify on site
        "title": (info.get("title") or "").strip(),
        "property_type": ", ".join(u["name"] for u in info.get("unitTypes", [])),
        "purpose": info.get("propertyFor"),
        "city": info.get("city"),
        "district": info.get("district"),
        "address": info.get("address"),
        "price_min": info.get("conversionPriceMin"),
        "price_max": info.get("conversionPriceMax"),
        "currency": info.get("currencyType") or "SAR",
        "units_available": (p.get("unitsAvailable") or {}).get("available")
                           if isinstance(p.get("unitsAvailable"), dict) else p.get("unitsAvailable"),
        "land_area_sqm": a.get("landArea"),
        "lat": to_num(loc.get("lat")),
        "lon": to_num(loc.get("lon")),
        "scraped_at": scraped_at,
    }


def iter_items(properties: list):
    for item in properties:
        if isinstance(item, list):  # carousels / grouped projects
            yield from (x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            yield item


def fmt_price(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else "on request"


def to_doc(r: dict) -> dict:
    if r["kind"] == "listing":
        parts = [
            f"{r['title']} for {r['purpose']} in {r['district']}, {r['city']}.",
            f"Price: {r['currency']} {fmt_price(r['price'])}.",
            f"Type: {r['property_type']}.",
            f"Bedrooms: {r['bedrooms']}." if r.get("bedrooms") else "",
            f"Bathrooms: {r['bathrooms']}." if r.get("bathrooms") else "",
            f"Built-up area: {r['built_up_area_sqm']} sqm." if r.get("built_up_area_sqm") else "",
            f"Land area: {r['land_area_sqm']} sqm." if r.get("land_area_sqm") else "",
            f"Age: {r['age']}." if r.get("age") else "",
            f"Facing: {r['facing']}." if r.get("facing") else "",
            f"Address: {r['address']}.",
        ]
    else:
        parts = [
            f"Project {r['title']} in {r['district']}, {r['city']} ({r['property_type']}).",
            f"Prices from {r['currency']} {fmt_price(r['price_min'])} to {fmt_price(r['price_max'])}.",
            f"Units available: {r['units_available']}." if r.get("units_available") else "",
            f"Address: {r['address']}.",
        ]
    return {
        "id": f"wasalt-{r['kind']}-{r['id']}",
        "source": "wasalt",
        "kind": r["kind"],
        "title": r["title"],
        "url": r["url"],
        "metadata": {k: r.get(k) for k in ("city", "district", "property_type", "price",
                                           "price_min", "price_max", "bedrooms") if r.get(k) is not None},
        "text": " ".join(x for x in parts if x),
    }


# ---------------------------------------------------------------- main
def search_url(city_id: int, ptype: str, page: int) -> str:
    q = {"countryId": 1, "propertyFor": "sale", "type": "residential",
         "propertyTypeData": ptype, "cityId": city_id}
    if page > 1:
        q["page"] = page
    return f"{BASE}/en/sale/search?{urlencode(q)}"


def main() -> None:
    max_pages = int(arg("--max-pages", 4))
    cdp = arg("--cdp")
    cdp_url = (cdp if isinstance(cdp, str) else "http://localhost:9222") if cdp else None
    OUT.mkdir(exist_ok=True)
    scraped_at = datetime.now(timezone.utc).isoformat()
    records: dict[str, dict] = {}
    f = Fetcher(cdp_url)

    try:
        for city, cid in CITIES.items():
            for ptype in PROPERTY_TYPES:
                prev_ids: set = set()
                for page in range(1, max_pages + 1):
                    url = search_url(cid, ptype, page)
                    print(f"{city} / {ptype} / page {page}")
                    d = f.next_data(url)
                    if not d:
                        print("  ! no data, skipping this search")
                        break
                    pp = d["props"]["pageProps"]
                    sr = pp.get("searchResult") or {}
                    if page == 1:
                        print(f"  total on site: {sr.get('count')}, pages: {sr.get('totalPages')}")
                    got_page = (pp.get("filterQueries") or {}).get("page")
                    ids, new = set(), 0
                    for item in iter_items(sr.get("properties") or []):
                        r = parse_listing(item, scraped_at) or parse_project(item, scraped_at)
                        if not r:
                            continue
                        key = f"{r['kind']}-{r['id']}"
                        ids.add(key)
                        if key not in records:
                            records[key] = r
                            new += 1
                    print(f"  {len(ids)} items, {new} new (server page index: {got_page})")
                    if not ids or ids == prev_ids:
                        print("  pagination stopped returning new results")
                        break
                    prev_ids = ids
                    time.sleep(random.uniform(2, 4))
    finally:
        f.close()

    rows = list(records.values())
    (OUT / "wasalt_listings.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    with open(OUT / "wasalt_docs.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(to_doc(r), ensure_ascii=False) + "\n")
    n_list = sum(r["kind"] == "listing" for r in rows)
    print(f"Saved {n_list} listings + {len(rows) - n_list} projects -> {OUT}/")


if __name__ == "__main__":
    main()
