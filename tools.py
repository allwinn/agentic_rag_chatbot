"""
Data-access layer for the agent: plain, typed, testable functions.
The agent never writes SQL; it calls these with typed filters, and every query is parameterised.

  search_wasalt_listings   SQLite  exact filters on Wasalt listings (price, beds, area, district…)
  wasalt_market_stats      SQLite  count / avg / min / max / price-per-sqm, optionally per district
  search_darglobal_projects SQLite DarGlobal projects by country, city, type, completion year, status
  search_knowledge         LanceDB hybrid search (vector + BM25, merged with Reciprocal Rank Fusion)
  calculate / mortgage_payment / convert_gulf_currency   deterministic maths, so the LLM never guesses numbers
"""
from __future__ import annotations

import ast
import json
import operator
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import lancedb

from embeddings import embed_query

DATA = Path(__file__).parent / "data"
DB_PATH = DATA / "properties.db"
LANCE_DIR = DATA / "lancedb"
MAX_ROWS = 15


@lru_cache(maxsize=1)
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


@lru_cache(maxsize=1)
def _chunks():
    return lancedb.connect(LANCE_DIR).open_table("chunks")


# ------------------------------------------------------------------ input normalisation
# The model may write a place or type in many ways ("United Arab Emirates", "KSA", "فيلا").
# Map them to the spelling stored in the database before filtering, so a synonym never
# silently returns zero rows.
_COUNTRY = {
    "uae": "UAE", "u.a.e": "UAE", "united arab emirates": "UAE", "emirates": "UAE", "الإمارات": "UAE",
    "الامارات": "UAE", "ksa": "Saudi Arabia", "saudi": "Saudi Arabia", "saudi arabia": "Saudi Arabia",
    "kingdom of saudi arabia": "Saudi Arabia", "السعودية": "Saudi Arabia", "uk": "UK",
    "united kingdom": "UK", "england": "UK", "britain": "UK", "great britain": "UK",
    "المملكة المتحدة": "UK", "oman": "Oman", "عمان": "Oman", "عُمان": "Oman", "qatar": "Qatar",
    "قطر": "Qatar", "spain": "Spain", "إسبانيا": "Spain", "اسبانيا": "Spain",
}
# City -> country, so a city search also includes that country's location guide
_CITY_COUNTRY = {"Dubai": "UAE", "Abu Dhabi": "UAE", "RAK": "UAE", "Riyadh": "Saudi Arabia",
                 "Jeddah": "Saudi Arabia", "Muscat": "Oman", "Doha": "Qatar", "London": "UK",
                 "Marbella": "Spain"}

_CITY = {"الرياض": "Riyadh", "riyadh": "Riyadh", "جدة": "Jeddah", "جده": "Jeddah", "jeddah": "Jeddah",
         "jedda": "Jeddah", "دبي": "Dubai", "dubai": "Dubai",
         "ras al khaimah": "RAK", "ras al-khaimah": "RAK", "rak": "RAK", "رأس الخيمة": "RAK",
         "al marjan island": "RAK", "marjan island": "RAK", "مسقط": "Muscat", "الدوحة": "Doha", "لندن": "London"}
_TYPE = {"villa": "Villa", "villas": "Villa", "فيلا": "Villa", "فلل": "Villa", "فيلل": "Villa",
         "townhouse": "Villa", "apartment": "Apartment", "apartments": "Apartment", "flat": "Apartment",
         "flats": "Apartment", "شقة": "Apartment", "شقق": "Apartment", "hotel": "Hotel", "hotel rooms": "Hotel"}


def _norm(value: Optional[str], table: dict) -> Optional[str]:
    if not value:
        return value
    v = value.strip()
    return table.get(v.lower(), table.get(v, v))


def _rows(sql: str, params: list) -> list[dict]:
    return [dict(r) for r in _db().execute(sql, params).fetchall()]


def data_scope() -> dict:
    """Small summary of what the data covers, used in the system prompt."""
    report = DATA / "index_report.json"
    r = json.loads(report.read_text()) if report.exists() else {}
    cities = _rows("SELECT DISTINCT city FROM wasalt_listings ORDER BY city", [])
    types = _rows("SELECT DISTINCT property_type FROM wasalt_listings ORDER BY property_type", [])
    countries = _rows("SELECT DISTINCT country FROM darglobal_projects ORDER BY country", [])
    return {
        "wasalt_cities": [c["city"] for c in cities],
        "wasalt_types": [t["property_type"] for t in types],
        "darglobal_countries": [c["country"] for c in countries],
        "scraped_at": (r.get("data_scraped_at") or "")[:10],
        "wasalt_listings": r.get("wasalt_listings"),
        "darglobal_projects": r.get("darglobal_projects"),
    }


# ------------------------------------------------------------------ Wasalt (SQL)
SortBy = Literal["price_asc", "price_desc", "price_per_sqm_asc", "area_desc", "newest"]
_SORT = {
    "price_asc": "price ASC", "price_desc": "price DESC",
    "price_per_sqm_asc": "price_per_sqm ASC", "area_desc": "COALESCE(built_up_area_sqm, land_area_sqm) DESC",
    "newest": "published_at DESC",
}


def search_wasalt_listings(
    city: Optional[str] = None,
    property_type: Optional[str] = None,
    district: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_bedrooms: Optional[int] = None,
    max_bedrooms: Optional[int] = None,
    min_area_sqm: Optional[float] = None,
    max_area_sqm: Optional[float] = None,
    sort_by: SortBy = "price_asc",
    limit: int = 8,
) -> dict:
    city, property_type = _norm(city, _CITY), _norm(property_type, _TYPE)
    where, p = ["price IS NOT NULL"], []
    if city:
        where.append("city LIKE ?"); p.append(f"%{city}%")
    if property_type:
        where.append("property_type LIKE ?"); p.append(f"%{property_type}%")
    if district:
        where.append("district LIKE ?"); p.append(f"%{district}%")
    if min_price is not None:
        where.append("price >= ?"); p.append(min_price)
    if max_price is not None:
        where.append("price <= ?"); p.append(max_price)
    if min_bedrooms is not None:
        where.append("bedrooms >= ?"); p.append(min_bedrooms)
    if max_bedrooms is not None:
        where.append("bedrooms <= ?"); p.append(max_bedrooms)
    if min_area_sqm is not None:
        where.append("COALESCE(built_up_area_sqm, land_area_sqm) >= ?"); p.append(min_area_sqm)
    if max_area_sqm is not None:
        where.append("COALESCE(built_up_area_sqm, land_area_sqm) <= ?"); p.append(max_area_sqm)
    w = " AND ".join(where)
    total = _db().execute(f"SELECT COUNT(*) FROM wasalt_listings WHERE {w}", p).fetchone()[0]
    rows = _rows(
        f"""SELECT title, property_type, city, district, price, currency, bedrooms, bathrooms,
                   built_up_area_sqm, land_area_sqm, price_per_sqm, age, url, image, lat, lon
            FROM wasalt_listings WHERE {w} ORDER BY {_SORT[sort_by]} LIMIT ?""",
        p + [max(1, min(limit, MAX_ROWS))])
    return {"total_matching": total, "shown": len(rows), "listings": rows}


DistrictSort = Literal["listings", "price_per_sqm_asc", "price_per_sqm_desc", "avg_price_asc", "avg_price_desc"]
_DISTRICT_SORT = {
    "listings": "listings DESC",
    "price_per_sqm_asc": "avg_price_per_sqm ASC", "price_per_sqm_desc": "avg_price_per_sqm DESC",
    "avg_price_asc": "avg_price ASC", "avg_price_desc": "avg_price DESC",
}


def wasalt_market_stats(
    city: Optional[str] = None,
    property_type: Optional[str] = None,
    district: Optional[str] = None,
    bedrooms: Optional[int] = None,
    group_by_district: bool = False,
    sort_districts_by: DistrictSort = "listings",
    top_n: int = 8,
) -> dict:
    city, property_type = _norm(city, _CITY), _norm(property_type, _TYPE)
    where, p = ["price IS NOT NULL"], []
    if city:
        where.append("city LIKE ?"); p.append(f"%{city}%")
    if property_type:
        where.append("property_type LIKE ?"); p.append(f"%{property_type}%")
    if district:
        where.append("district LIKE ?"); p.append(f"%{district}%")
    if bedrooms is not None:
        where.append("bedrooms = ?"); p.append(bedrooms)
    w = " AND ".join(where)
    select = """COUNT(*) AS listings, ROUND(AVG(price)) AS avg_price, MIN(price) AS min_price,
                MAX(price) AS max_price, ROUND(AVG(price_per_sqm)) AS avg_price_per_sqm"""
    if group_by_district:
        order = _DISTRICT_SORT.get(sort_districts_by, "listings DESC")
        # ranking by price/m2 needs >= 2 listings that HAVE a price/m2 (some listings have no area),
        # otherwise one listing's figure would be presented as a district average
        per_sqm = 1 if sort_districts_by.startswith("price_per_sqm") else 0
        rows = _rows(f"""SELECT city, district, {select} FROM wasalt_listings WHERE {w}
                         GROUP BY city, district HAVING COUNT(*) >= 2
                         AND (? = 0 OR COUNT(price_per_sqm) >= 2)
                         ORDER BY {order} LIMIT ?""", p + [per_sqm, max(1, min(top_n, 15))])
        return {"currency": "SAR", "by_district": rows,
                "note": "Districts with fewer than 2 listings are omitted. Sample data, not the full market."}
    row = _rows(f"SELECT {select} FROM wasalt_listings WHERE {w}", p)[0]
    return {"currency": "SAR", **row, "note": "Based on the scraped sample, not the full market."}


# ------------------------------------------------------------------ DarGlobal (SQL)
def search_darglobal_projects(
    country: Optional[str] = None,
    city: Optional[str] = None,
    project_type: Optional[str] = None,
    completion_year_from: Optional[int] = None,
    completion_year_to: Optional[int] = None,
    status: Optional[Literal["Off-plan", "Sold out", "Completed", "Completion date passed", "Unknown"]] = None,
    bedrooms: Optional[int] = None,
    name_contains: Optional[str] = None,
) -> dict:
    country, city, project_type = _norm(country, _COUNTRY), _norm(city, _CITY), _norm(project_type, _TYPE)
    where, p = ["1=1"], []
    if country:
        where.append("country LIKE ?"); p.append(f"%{country}%")
    if city:
        where.append("(city LIKE ? OR location LIKE ?)"); p += [f"%{city}%", f"%{city}%"]
    if project_type:
        where.append("project_type LIKE ?"); p.append(f"%{project_type}%")
    if completion_year_from is not None:
        where.append("completion_year >= ?"); p.append(completion_year_from)
    if completion_year_to is not None:
        where.append("completion_year <= ?"); p.append(completion_year_to)
    if status:
        where.append("status = ?"); p.append(status)
    if bedrooms is not None:
        where.append("bedrooms_min <= ? AND bedrooms_max >= ?"); p += [bedrooms, bedrooms]
    if name_contains:
        where.append("title LIKE ?"); p.append(f"%{name_contains}%")
    rows = _rows(f"""SELECT title, country, location, project_type, bedrooms_text, completion,
                            status, url, image FROM darglobal_projects WHERE {' AND '.join(where)}
                     ORDER BY country, completion_year""", p)
    return {"count": len(rows), "projects": rows,
            "note": "DarGlobal does not publish unit prices on most project pages."}


# Fields used only by the UI (cards, map). The agent strips them before the model sees results.
UI_ONLY_FIELDS = ("image", "lat", "lon")


def strip_ui_fields(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in UI_ONLY_FIELDS} for r in rows]


# ------------------------------------------------------------------ hybrid search (LanceDB)
def _sql_str(v: str) -> str:
    return v.replace("'", "''")


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_CMS_NOISE = re.compile(r"(?i)(image update|webp|hero banner|^[a-z0-9]+(-[a-z0-9]+){2,}$)")
PASSAGE_CHARS = 900  # per passage sent to the model (keeps a 4-passage search under ~1,000 tokens)


def _clean_passage(text: str) -> str:
    """Drop CMS leftovers (image ids, upload notes, url slugs) and trim to PASSAGE_CHARS."""
    lines = [l for l in text.split("\n")
             if l.strip() and not _UUID.match(l.strip()) and not _CMS_NOISE.search(l.strip())]
    out = "\n".join(lines)
    return out if len(out) <= PASSAGE_CHARS else out[:PASSAGE_CHARS].rsplit(" ", 1)[0] + " …"


def search_knowledge(
    query: str,
    source: Optional[Literal["darglobal", "wasalt"]] = None,
    city: Optional[str] = None,
    limit: int = 4,
    per_doc: int = 1,
) -> dict:
    """Hybrid retrieval: vector (meaning) + BM25 (exact words), fused with Reciprocal Rank Fusion."""
    tbl = _chunks()
    filters = []
    if source:
        filters.append(f"source = '{_sql_str(source)}'")
    if city:
        c = _sql_str(_norm(city, _CITY))
        if "location" in tbl.schema.names:
            # Match where the passage IS (its project/listing location), not every page that merely
            # mentions the city; plus that country's location guide.
            cond = f"city = '{c}' OR location LIKE '%{c}%'"
            country = _CITY_COUNTRY.get(c)
            if country:
                cond += f" OR (kind = 'location_guide' AND country = '{_sql_str(country)}')"
            filters.append(f"({cond})")
        else:  # index built before the location column existed
            filters.append(f"(city = '{c}' OR text LIKE '%{c}%')")
    where = " AND ".join(filters) or None
    k = 20
    cols = ["id", "doc_id", "source", "kind", "title", "url", "city", "text"]
    vcols, fcols = cols + ["_distance"], cols + ["_score"]  # include scores explicitly (avoids Lance warnings)

    vq = tbl.search(embed_query(query).tolist()).select(vcols).limit(k)
    if where:
        vq = vq.where(where, prefilter=True)
    vec_hits = vq.to_list()
    try:
        fq = tbl.search(query, query_type="fts").select(fcols).limit(k)
        if where:
            fq = fq.where(where, prefilter=True)
        kw_hits = fq.to_list()
    except Exception:  # FTS can reject odd punctuation; fall back to vector only
        kw_hits = []

    scores, by_id = {}, {}
    for hits in (vec_hits, kw_hits):
        for rank, h in enumerate(hits):
            scores[h["id"]] = scores.get(h["id"], 0.0) + 1.0 / (60 + rank)
            by_id[h["id"]] = h
    ranked = sorted(scores, key=scores.get, reverse=True)

    results, seen_docs = [], {}
    for cid in ranked:
        h = by_id[cid]
        if seen_docs.get(h["doc_id"], 0) >= per_doc:  # diversity: one passage per document by default
            continue
        seen_docs[h["doc_id"]] = seen_docs.get(h["doc_id"], 0) + 1
        results.append({"title": h["title"], "source": h["source"], "kind": h["kind"],
                        "url": h["url"], "text": _clean_passage(h["text"])})
        if len(results) >= limit:
            break
    return {"query": query, "results": results}


# ------------------------------------------------------------------ contact details
CONTACT_FILE = DATA / "darglobal_contact.json"


def get_contact_info(source: str, url: Optional[str] = None, name: Optional[str] = None) -> dict:
    """Official contact routes. DarGlobal: published company contacts. Wasalt: the listing page,
    because advertiser phone numbers are personal data and are deliberately not stored."""
    if source == "darglobal":
        c = json.loads(CONTACT_FILE.read_text()) if CONTACT_FILE.exists() else {}
        if not c:
            return {"error": "DarGlobal contact data not available", "website": "https://darglobal.co.uk"}
        out = {k: c[k] for k in ("company", "customer_service_phone", "whatsapp", "languages",
                                 "head_office", "website", "how_to_enquire_about_a_project") if c.get(k)}
        if name:
            rows = _rows("SELECT title, url FROM darglobal_projects WHERE title LIKE ? LIMIT 1", [f"%{name}%"])
            if rows:
                out["project_page"] = rows[0]["url"]
                out["project"] = rows[0]["title"]
        out["note"] = "Official company contacts published on darglobal.co.uk."
        return out
    if source == "wasalt":
        out = {"how_to_contact": ("Open the listing on Wasalt and use its Call / WhatsApp / Message "
                                  "buttons to reach the advertiser (owner or licensed broker)."),
               "note": ("Advertiser phone numbers and names are personal data; this assistant does not "
                        "store or share them.")}
        if url:
            out["listing_page"] = url
        return out
    return {"error": 'source must be "darglobal" or "wasalt"'}


# ------------------------------------------------------------------ deterministic maths
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def calculate(expression: str) -> dict:
    """Safely evaluate arithmetic like '3200000 / 450' or '(1.2e6 - 950000) / 950000 * 100'."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only numbers and + - * / ** % ( ) are allowed")
    try:
        value = ev(ast.parse(expression.replace(",", ""), mode="eval").body)
        return {"expression": expression, "result": round(value, 4)}
    except Exception as e:
        return {"expression": expression, "error": str(e)}


def mortgage_payment(price: float, down_payment_pct: float = 20, annual_rate_pct: float = 5.5,
                     years: int = 25) -> dict:
    """Standard amortised monthly payment. Rates are illustrative, not offers."""
    loan = price * (1 - down_payment_pct / 100)
    r, n = annual_rate_pct / 100 / 12, years * 12
    monthly = loan / n if r == 0 else loan * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return {"price": price, "down_payment": round(price - loan), "loan_amount": round(loan),
            "annual_rate_pct": annual_rate_pct, "years": years, "monthly_payment": round(monthly),
            "total_interest": round(monthly * n - loan),
            "note": "Illustrative estimate; actual bank terms vary."}


# Gulf currencies are pegged to the US dollar, so these rates are fixed, not market quotes.
_USD_PEG = {"USD": 1.0, "SAR": 3.75, "AED": 3.6725, "QAR": 3.64, "OMR": 0.3845, "BHD": 0.376}


def convert_gulf_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    f, t = from_currency.upper(), to_currency.upper()
    if f not in _USD_PEG or t not in _USD_PEG:
        return {"error": f"Only USD-pegged currencies are supported: {', '.join(_USD_PEG)}"}
    return {"amount": amount, "from": f, "to": t,
            "result": round(amount / _USD_PEG[f] * _USD_PEG[t], 2), "basis": "official USD pegs"}
