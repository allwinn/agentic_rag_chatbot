"""
Build the chatbot's two stores from the scraped files in data/.

  data/properties.db   SQLite  -> exact filters (price, city, bedrooms, completion year...)
  data/lancedb/        LanceDB -> semantic + keyword (hybrid) search over text chunks
  data/index_report.json        counts for the README / sanity checks

Run once after scraping:   python build_index.py
Both stores are copied into the Docker image, so the app never scrapes or embeds at startup.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from embeddings import BACKEND, DIM, MODEL_NAME, embed

DATA = Path("data")
DB_PATH = DATA / "properties.db"
LANCE_DIR = DATA / "lancedb"
TABLE = "chunks"

CHUNK_CHARS = 3200      # ~800 tokens
OVERLAP_CHARS = 400     # ~100 tokens
BOILERPLATE_SHARE = 0.4  # a line on >=40% of DarGlobal project pages is site chrome
NON_RESIDENTIAL = {"Building", "Warehouse", "Land", "Office", "Shop", "Showroom"}

COUNTRY_ALIASES = {"United Arab Emirates": "UAE", "Saudi Arabia": "Saudi Arabia",
                   "United Kingdom": "UK", "Oman": "Oman", "Qatar": "Qatar", "Spain": "Spain"}


# ------------------------------------------------------------------ helpers
def load_json(path: Path) -> list:
    if not path.exists():
        print(f"  ! missing {path}, skipping")
        return []
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> list:
    if not path.exists():
        print(f"  ! missing {path}, skipping")
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def bedroom_range(text: str | None) -> tuple[int | None, int | None]:
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    return (min(nums), max(nums)) if nums else (None, None)


def year_of(text: str | None) -> int | None:
    m = re.search(r"(20\d\d)", text or "")
    return int(m.group(1)) if m else None


def project_status(sold_out: bool, completion: str | None) -> str:
    if sold_out:
        return "Sold out"
    if (completion or "").strip().lower() == "completed":
        return "Completed"
    y = year_of(completion)
    if y and y < datetime.now().year:
        return "Completion date passed"  # scheduled in the past; handover not confirmed by the site
    return "Off-plan" if y else "Unknown"


def first_city(location: str | None) -> str | None:
    if not location:
        return None
    parts = [p.strip() for p in re.split(r"[,|]", location) if p.strip()]
    return parts[0] if parts else None


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split on line boundaries into ~size-char chunks with overlap."""
    lines, chunks, cur = text.splitlines(), [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > size and cur:
            chunks.append(cur.strip())
            cur = cur[-overlap:]  # carry the tail over for context
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [text[:size]]


# ------------------------------------------------------------------ cleaning
def clean_darglobal(projects: list, docs: list):
    rows = []
    for p in projects:
        bmin, bmax = bedroom_range(p.get("bedrooms"))
        sold_out = "sold out" in (p.get("bedrooms") or "").lower()
        completion = p.get("completion")
        rows.append({
            "id": p["url"].rstrip("/").split("/")[-1],
            "title": p["title"].strip(),
            "country": COUNTRY_ALIASES.get(p.get("country"), p.get("country")),
            "city": first_city(p.get("location")),
            "location": p.get("location"),
            "project_type": p.get("project_type"),
            "bedrooms_text": None if sold_out else (p.get("bedrooms") or None),
            "bedrooms_min": None if sold_out else bmin,
            "bedrooms_max": None if sold_out else bmax,
            "completion": completion,
            "completion_year": year_of(completion),
            "status": project_status(sold_out, completion),
            "url": p["url"],
            "image": p.get("image"),
            "scraped_at": p.get("scraped_at"),
        })
        # DarGlobal "prices_found" are mostly shared marketing figures: deliberately not stored.

    project_docs = [d for d in docs if d.get("kind") == "project"]
    other_docs = [d for d in docs if d.get("kind") != "project"]

    # 1) strip site chrome: lines repeated across many project pages
    line_freq = Counter()
    for d in project_docs:
        line_freq.update(set(l.strip() for l in d["text"].splitlines() if l.strip()))
    cutoff = max(3, int(len(project_docs) * BOILERPLATE_SHARE))
    boilerplate = {l for l, n in line_freq.items() if n >= cutoff}
    for d in project_docs:
        d["text"] = "\n".join(l for l in d["text"].splitlines() if l.strip() and l.strip() not in boilerplate)

    # 2) merge pages with identical text (the one-of-one pages share one collection payload)
    by_hash: dict[str, dict] = {}
    for d in project_docs:
        h = hashlib.md5(d["text"].encode()).hexdigest()
        if h in by_hash:
            by_hash[h].setdefault("also", []).append(d["title"])
            by_hash[h].setdefault("also_loc", []).append((d.get("metadata") or {}).get("location"))
        else:
            by_hash[h] = d
    merged = []
    for d in by_hash.values():
        if d.get("also"):
            names = [d["title"]] + d["also"]
            locs = [(d.get("metadata") or {}).get("location")] + d.get("also_loc", [])
            locs = "; ".join(dict.fromkeys(l for l in locs if l)) or "Various locations"
            # one shared page covering several projects: label it with all their locations, and no
            # single country/city, so a city filter doesn't treat it as e.g. a Dubai-only project
            d = {**d, "title": "One of One collection: " + "; ".join(names),
                 "metadata": {**(d.get("metadata") or {}), "projects": names,
                              "location": locs, "country": ""}}
        merged.append(d)
    stats = {"boilerplate_lines_removed": len(boilerplate),
             "duplicate_pages_merged": len(project_docs) - len(merged)}
    return rows, merged + other_docs, stats


def clean_wasalt(rows: list, docs: list):
    keep_ids = set()
    listings, projects = [], []
    for r in rows:
        if r["kind"] == "listing":
            if r.get("property_type") in NON_RESIDENTIAL:
                continue
            listings.append(r)
        else:
            projects.append(r)
        keep_ids.add(f"wasalt-{r['kind']}-{r['id']}")
    docs = [d for d in docs if d["id"] in keep_ids]
    return listings, projects, docs


# ------------------------------------------------------------------ SQLite
SCHEMA = """
DROP TABLE IF EXISTS darglobal_projects;
CREATE TABLE darglobal_projects (
  id TEXT PRIMARY KEY, title TEXT, country TEXT, city TEXT, location TEXT,
  project_type TEXT, bedrooms_text TEXT, bedrooms_min INTEGER, bedrooms_max INTEGER,
  completion TEXT, completion_year INTEGER, status TEXT, url TEXT, image TEXT, scraped_at TEXT);

DROP TABLE IF EXISTS wasalt_listings;
CREATE TABLE wasalt_listings (
  id INTEGER PRIMARY KEY, title TEXT, property_type TEXT, purpose TEXT, city TEXT,
  district TEXT, territory TEXT, address TEXT, price REAL, currency TEXT,
  bedrooms INTEGER, bathrooms INTEGER, built_up_area_sqm REAL, land_area_sqm REAL,
  price_per_sqm REAL, age TEXT, facing TEXT, furnishing TEXT, lat REAL, lon REAL,
  url TEXT, image TEXT, published_at TEXT, scraped_at TEXT);

DROP TABLE IF EXISTS wasalt_projects;
CREATE TABLE wasalt_projects (
  id INTEGER PRIMARY KEY, title TEXT, unit_types TEXT, city TEXT, district TEXT,
  address TEXT, price_min REAL, price_max REAL, currency TEXT, units_available INTEGER,
  lat REAL, lon REAL, url TEXT, scraped_at TEXT);

CREATE INDEX idx_wl_filter ON wasalt_listings (city, property_type, bedrooms, price);
CREATE INDEX idx_wl_district ON wasalt_listings (district);
CREATE INDEX idx_dg_filter ON darglobal_projects (country, city, project_type, completion_year);
"""


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def build_sqlite(dg_rows, w_listings, w_projects) -> None:
    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO darglobal_projects VALUES (:id,:title,:country,:city,:location,:project_type,"
        ":bedrooms_text,:bedrooms_min,:bedrooms_max,:completion,:completion_year,:status,:url,:image,:scraped_at)",
        dg_rows)
    for r in w_listings:
        area = r.get("built_up_area_sqm") or r.get("land_area_sqm")
        con.execute(
            "INSERT OR REPLACE INTO wasalt_listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r.get("title"), r.get("property_type"), r.get("purpose"), r.get("city"),
             r.get("district"), r.get("territory"), r.get("address"), r.get("price"), r.get("currency"),
             to_int(r.get("bedrooms")), to_int(r.get("bathrooms")), r.get("built_up_area_sqm"),
             r.get("land_area_sqm"),
             round(r["price"] / area, 0) if r.get("price") and area else None,
             str(r["age"]) if r.get("age") is not None else None, r.get("facing"), r.get("furnishing"),
             r.get("lat"), r.get("lon"), r.get("url"), r.get("image"), r.get("published_at"),
             r.get("scraped_at")))
    for r in w_projects:
        con.execute(
            "INSERT OR REPLACE INTO wasalt_projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r.get("title"), r.get("property_type"), r.get("city"), r.get("district"),
             r.get("address"), r.get("price_min"), r.get("price_max"), r.get("currency"),
             to_int(r.get("units_available")), r.get("lat"), r.get("lon"), r.get("url"),
             r.get("scraped_at")))
    con.commit()
    con.close()


# ------------------------------------------------------------------ LanceDB
def make_chunks(dg_docs: list, w_docs: list) -> list[dict]:
    chunks = []
    for d in dg_docs:
        md = d.get("metadata") or {}
        header = f"{d['title']}"
        if md.get("location"):
            header += f" | {md['location']}"
        header += " | DarGlobal " + ("project" if d["kind"] == "project" else "location guide")
        for i, body in enumerate(chunk_text(d["text"])):
            chunks.append({
                "id": f"{d['id']}#{i}", "doc_id": d["id"], "source": "darglobal", "kind": d["kind"],
                "title": d["title"], "url": d["url"],
                "city": "" if md.get("projects") else (first_city(md.get("location")) or ""), "country": COUNTRY_ALIASES.get(md.get("country"), md.get("country") or ""),
                "price": None,
                "text": f"{header}\n{body}",  # contextual header: every chunk knows its project
            })
    for d in w_docs:  # one-line summaries: one chunk each
        md = d.get("metadata") or {}
        chunks.append({
            "id": d["id"], "doc_id": d["id"], "source": "wasalt", "kind": d["kind"],
            "title": d["title"] or "", "url": d["url"],
            "city": md.get("city") or "", "country": "Saudi Arabia",
            "price": float(md["price"]) if md.get("price") else None,
            "text": d["text"],
        })
    return chunks


def build_lance(chunks: list[dict]) -> None:
    print(f"  embedding {len(chunks)} chunks with {MODEL_NAME} ({BACKEND})…")
    vecs = embed([c["text"] for c in chunks])
    schema = pa.schema([
        ("id", pa.string()), ("doc_id", pa.string()), ("source", pa.string()), ("kind", pa.string()),
        ("title", pa.string()), ("url", pa.string()), ("city", pa.string()), ("country", pa.string()),
        ("price", pa.float64()), ("text", pa.string()), ("vector", pa.list_(pa.float32(), DIM)),
    ])
    data = [{**c, "vector": v.tolist()} for c, v in zip(chunks, vecs)]
    db = lancedb.connect(LANCE_DIR)
    tbl = db.create_table(TABLE, data=data, schema=schema, mode="overwrite")
    tbl.create_index("text", config=FTS(), replace=True)  # BM25 keyword index for hybrid search


# ------------------------------------------------------------------ main
def main() -> None:
    print("Loading scraped files…")
    dg_projects = load_json(DATA / "darglobal_projects.json")
    dg_docs = load_jsonl(DATA / "darglobal_docs.jsonl")
    w_rows = load_json(DATA / "wasalt_listings.json")
    w_docs = load_jsonl(DATA / "wasalt_docs.jsonl")

    print("Cleaning…")
    dg_rows, dg_docs, dg_stats = clean_darglobal(dg_projects, dg_docs)
    w_listings, w_projects, w_docs = clean_wasalt(w_rows, w_docs)

    print("Building SQLite…")
    build_sqlite(dg_rows, w_listings, w_projects)

    print("Building LanceDB…")
    chunks = make_chunks(dg_docs, w_docs)
    build_lance(chunks)

    report = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": MODEL_NAME, "embedding_backend": BACKEND, "dim": DIM,
        "darglobal_projects": len(dg_rows),
        "darglobal_projects_by_country": Counter(r["country"] for r in dg_rows),
        "darglobal_docs_after_dedupe": len(dg_docs), **dg_stats,
        "wasalt_listings": len(w_listings),
        "wasalt_listings_by_city_type": {f"{k[0]} / {k[1]}": v for k, v in
                                         Counter((r["city"], r["property_type"]) for r in w_listings).items()},
        "wasalt_projects": len(w_projects),
        "wasalt_dropped_non_residential": sum(1 for r in w_rows if r["kind"] == "listing") - len(w_listings),
        "chunks_total": len(chunks),
        "chunks_by_source": Counter(c["source"] for c in chunks),
        "data_scraped_at": max((r.get("scraped_at") or "" for r in w_listings + dg_rows), default=None),
    }
    (DATA / "index_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if BACKEND == "hash":
        print("\n!! Built with the HASH test embedder. Re-run without EMBED_BACKEND=hash for the real index.")


if __name__ == "__main__":
    main()
