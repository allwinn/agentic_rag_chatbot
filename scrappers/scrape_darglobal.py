"""
DarGlobal scraper.

DarGlobal is a Next.js + Strapi site: every page embeds its full data as JSON in
<script id="__NEXT_DATA__">. One listing page contains ALL projects (grouped in
country tabs), so we:
  1. fetch the listing page -> parse project cards
  2. fetch each project's detail page -> keep its pageProps + flattened text
  3. write data/darglobal_projects.json (structured) and darglobal_docs.jsonl (for RAG)

Usage:  pip install httpx selectolax
        python scrape_darglobal.py
"""
from __future__ import annotations  # allows `dict | None` hints on Python 3.8/3.9

import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = "https://darglobal.co.uk"
LISTING_URL = f"{BASE}/projects/properties-in-uae/apartments"
OUT = Path("data")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY_S = 1.5  # be polite


def next_data(client: httpx.Client, url: str) -> dict | None:
    r = client.get(url)
    if r.status_code != 200:
        print(f"  ! {r.status_code} {url}")
        return None
    node = HTMLParser(r.text).css_first("script#__NEXT_DATA__")
    return json.loads(node.text()) if node else None


def fetch_page(client: httpx.Client, url: str) -> tuple[str, dict | None, str, str]:
    """Return (mode, pageProps-or-None, visible_text, final_url) for a detail page."""
    r = client.get(url)
    if r.status_code != 200:
        print(f"  ! {r.status_code} {url}")
        return "error", None, "", str(r.url)
    (OUT / "html").mkdir(parents=True, exist_ok=True)
    slug = url.rstrip("/").split("/")[-1]
    (OUT / "html" / f"{slug}.html").write_text(r.text)  # keep for debugging

    tree = HTMLParser(r.text)
    node = tree.css_first("script#__NEXT_DATA__")
    if node:
        d = json.loads(node.text())
        return "next_data", d["props"].get("pageProps", {}), "", str(r.url)

    # Fallback: structured data + visible text
    ld = [s.text() for s in tree.css('script[type="application/ld+json"]')]
    for sel in ("script", "style", "noscript", "svg", "header", "footer", "nav", "iframe"):
        for n in tree.css(sel):
            n.decompose()
    body = tree.body
    text = body.text(separator="\n") if body else ""
    lines, seen = [], set()
    for ln in (l.strip() for l in text.splitlines()):
        if len(ln) > 2 and ln not in seen:
            seen.add(ln)
            lines.append(ln)
    if ld:
        lines.append("STRUCTURED_DATA: " + " ".join(ld))
    return "html_text", None, "\n".join(lines), str(r.url)


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


SKIP_KEYS = {"url", "hash", "mime", "ext", "provider", "href", "createdAt", "updatedAt",
             "publishedAt", "locale", "name", "alternativeText", "metaRobots", "metaViewport"}


def flatten_text(obj, out: list[str], key: str = "") -> None:
    """Collect human-readable strings from an arbitrary Strapi payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS or k in ("formats", "globalData", "thankyouData"):
                continue
            flatten_text(v, out, k)
    elif isinstance(obj, list):
        for v in obj:
            flatten_text(v, out, key)
    elif isinstance(obj, str):
        t = strip_html(obj)
        if len(t) > 3 and not t.startswith(("http", "/", "#")) and t not in out:
            out.append(t)


def find_prices(text: str) -> list[str]:
    pat = r"(?:AED|SAR|QAR|OMR|GBP|EUR|USD|£|€|\$)\s?[\d,.]+\s?(?:[KkMm](?:illion)?)?"
    return sorted({m.strip() for m in re.findall(pat, text)})


def card_image(card: dict) -> str | None:
    try:
        return card["ProjectCardImage"]["data"]["attributes"]["url"]
    except (KeyError, TypeError):
        return None


def main() -> None:
    OUT.mkdir(exist_ok=True)
    scraped_at = datetime.now(timezone.utc).isoformat()

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        nd = next_data(client, LISTING_URL)
        attrs = nd["props"]["pageProps"]["pageData"]["attributes"]

        # 1) project cards from country tabs
        projects, seen = [], set()
        for tab in attrs["Tabs"]:
            for c in tab["TabData"]:
                if c["Link"] in seen:
                    continue
                seen.add(c["Link"])
                projects.append({
                    "source": "darglobal",
                    "country": tab["TabHeading"],
                    "title": c["ProjectTitle"].strip(),
                    "location": c["Location"],
                    "project_type": c["ProjectType"],
                    "bedrooms": (c.get("Bedrooms") or "").strip() or None,
                    "completion": c.get("completionDate"),
                    "status": "Sold Out" if "sold out" in (c.get("Bedrooms") or "").lower() else None,
                    "url": BASE + c["Link"],
                    "image": card_image(c),
                    "scraped_at": scraped_at,
                })
        print(f"Found {len(projects)} projects")

        # location-level SEO content (good RAG material)
        docs = []
        for block in attrs.get("allContents", []):
            for sc in block.get("seoContent", []):
                body = strip_html(sc.get("seoContent") or "")
                if body:
                    docs.append({
                        "id": f"darglobal-loc-{block['locationType']}-{sc['id']}",
                        "source": "darglobal",
                        "kind": "location_guide",
                        "title": sc.get("contentHeading") or block["locationType"],
                        "url": f"{BASE}/projects/{block['locationType']}",
                        "text": body,
                    })

        # 2) detail pages
        for i, p in enumerate(projects, 1):
            print(f"[{i}/{len(projects)}] {p['title']}")
            time.sleep(DELAY_S)
            mode, pp, text, final_url = fetch_page(client, p["url"])
            slug = p["url"].rstrip("/").split("/")[-1]
            if mode == "next_data":
                chunks: list[str] = []
                flatten_text(pp, chunks)
                text = "\n".join(chunks)
                (OUT / "raw").mkdir(exist_ok=True)
                (OUT / "raw" / f"{slug}.json").write_text(json.dumps(pp, ensure_ascii=False))
            print(f"    mode={mode} chars={len(text)}"
                  + (f" redirected->{final_url}" if final_url.rstrip('/') != p['url'].rstrip('/') else ""))
            p["detail_mode"] = mode
            if not text:
                continue
            p["prices_found"] = find_prices(text)
            docs.append({
                "id": f"darglobal-{slug}",
                "source": "darglobal",
                "kind": "project",
                "title": p["title"],
                "url": p["url"],
                "metadata": {k: p[k] for k in ("country", "location", "project_type",
                                               "bedrooms", "completion")},
                "text": text,
            })

    (OUT / "darglobal_projects.json").write_text(json.dumps(projects, indent=2, ensure_ascii=False))
    with open(OUT / "darglobal_docs.jsonl", "w") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print(f"Saved {len(projects)} projects, {len(docs)} docs -> {OUT}/")


if __name__ == "__main__":
    main()