"""
DarGlobal detail-page scraper (Playwright).

The listing step (scrape_darglobal.py) works with plain HTTP, but project pages sit
behind Imperva/Incapsula, which needs a real browser to run its JS check. This script:
  - reads data/darglobal_projects.json (from scrape_darglobal.py)
  - opens each project page in one real Chromium session, slowly
  - takes __NEXT_DATA__ if present, otherwise the visible page text
  - rewrites data/darglobal_projects.json + data/darglobal_docs.jsonl

Setup:  pip install playwright httpx selectolax
        playwright install chromium
Run:    python scrape_darglobal_details.py            (opens a visible browser window)
        python scrape_darglobal_details.py --headless (may get blocked more often)
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from scrape_darglobal import BASE, flatten_text, find_prices

OUT = Path("data")
PROJECTS_FILE = OUT / "darglobal_projects.json"
DOCS_FILE = OUT / "darglobal_docs.jsonl"
BLOCK_MARKER = "_Incapsula_Resource"

EXTRACT_JS = """
() => {
  const nd = document.querySelector('script#__NEXT_DATA__');
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,svg,header,footer,nav,iframe').forEach(n => n.remove());
  return {
    nextData: nd ? nd.textContent : null,
    text: clone.innerText || '',
    title: document.title,
  };
}
"""


def clean_lines(text: str) -> str:
    seen, out = set(), []
    for ln in (l.strip() for l in text.splitlines()):
        if len(ln) > 2 and ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "\n".join(out)


def load_page(page, url: str, tries: int = 2) -> dict | None:
    for attempt in range(1, tries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # give the Incapsula check time to run and redirect to the real page
        try:
            page.wait_for_function(
                "() => !document.documentElement.innerHTML.includes('_Incapsula_Resource')"
                " && document.body && document.body.innerText.length > 200",
                timeout=25_000,
            )
        except Exception:
            pass
        page.wait_for_timeout(1500)  # let late content render
        if BLOCK_MARKER in page.content() and attempt < tries:
            print("    blocked, waiting and retrying…")
            page.wait_for_timeout(8000)
            continue
        return page.evaluate(EXTRACT_JS)
    return None


def main() -> None:
    headless = "--headless" in sys.argv
    cdp_url = None
    if "--cdp" in sys.argv:
        i = sys.argv.index("--cdp")
        cdp_url = sys.argv[i + 1] if len(sys.argv) > i + 1 else "http://localhost:9222"
    projects = json.loads(PROJECTS_FILE.read_text())

    # keep location guides from the first run
    docs = []
    if DOCS_FILE.exists():
        docs = [d for d in map(json.loads, DOCS_FILE.read_text().splitlines())
                if d.get("kind") == "location_guide"]

    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    (OUT / "html").mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        if cdp_url:
            # attach to the user's own Chrome (started with --remote-debugging-port)
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            print(f"Connected to your Chrome at {cdp_url}")
        else:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            # warm up on the home page so the session gets its cookies once
            load_page(page, BASE)

        blocked_in_a_row = 0

        for i, p in enumerate(projects, 1):
            print(f"[{i}/{len(projects)}] {p['title']}")
            slug = p["url"].rstrip("/").split("/")[-1]
            res = load_page(page, p["url"])
            (OUT / "html" / f"{slug}.html").write_text(page.content())

            text, mode = "", "blocked"
            if res and res.get("nextData"):
                pp = json.loads(res["nextData"])["props"].get("pageProps", {})
                (OUT / "raw" / f"{slug}.json").write_text(json.dumps(pp, ensure_ascii=False))
                chunks: list[str] = []
                flatten_text(pp, chunks)
                text, mode = "\n".join(chunks), "next_data"
            elif res and len(res.get("text", "")) > 200:
                text, mode = clean_lines(res["text"]), "page_text"

            p["detail_mode"] = mode
            p["prices_found"] = find_prices(text) if text else []
            print(f"    mode={mode} chars={len(text)} prices={p['prices_found'][:3]}")
            blocked_in_a_row = blocked_in_a_row + 1 if mode == "blocked" else 0
            if blocked_in_a_row >= 3:
                print("Blocked 3 times in a row - stopping. Saving what we have.")
                break

            if text:
                docs.append({
                    "id": f"darglobal-{slug}",
                    "source": "darglobal",
                    "kind": "project",
                    "title": p["title"],
                    "url": p["url"],
                    "metadata": {k: p.get(k) for k in ("country", "location", "project_type",
                                                       "bedrooms", "completion")},
                    "text": text,
                })
            time.sleep(random.uniform(3, 6))  # polite pacing

        if not cdp_url:
            browser.close()  # never close the user's own Chrome
        else:
            page.close()

    PROJECTS_FILE.write_text(json.dumps(projects, indent=2, ensure_ascii=False))
    with open(DOCS_FILE, "w") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    ok = sum(p.get("detail_mode") not in (None, "blocked") for p in projects)
    print(f"Done: {ok}/{len(projects)} detail pages, {len(docs)} docs -> {OUT}/")


if __name__ == "__main__":
    main()