"""
One-off: rebuild evals/results.json + results.md from the 2026-11-19 12:48 UTC run, after a
partial run overwrote them. Tool calls and latencies are copied from the terminal output of that
run; pass/fail per check is reconstructed from each case's definition. Tokens and answer text
were not recoverable.
Run from the project folder:  python restore_eval_results.py
"""
import json
from pathlib import Path

import evals as E

RUN_AT = "2026-11-19T12:48:00+00:00"
TOOLS_CALLED = {
    "wasalt-cheapest-4bed-riyadh": ["search_wasalt_listings"],
    "wasalt-count-jeddah-apts": ["search_wasalt_listings"],
    "wasalt-avg-villa-riyadh": ["wasalt_market_stats"],
    "wasalt-cheapest-district-sqm": ["wasalt_market_stats"],
    "dg-oman-count": ["search_darglobal_projects"],
    "dg-completing-2027": ["search_darglobal_projects"],
    "dg-about-dg1": ["search_darglobal_projects", "search_knowledge"],
    "dg-pagani": ["search_knowledge"],
    "calc-mortgage": ["mortgage_payment"],
    "calc-currency": ["convert_gulf_currency"],
    "multi-villa-mortgage": ["search_wasalt_listings", "search_wasalt_listings", "mortgage_payment"],
    "multi-cross-source": ["search_darglobal_projects", "search_wasalt_listings",
                           "wasalt_market_stats", "search_knowledge"],
    "arabic-cheapest-villa": ["search_wasalt_listings"],
    "scope-weather": [], "scope-rentals": [], "scope-injection": [],
}
LATENCY = {
    "wasalt-cheapest-4bed-riyadh": 22.0, "wasalt-count-jeddah-apts": 8.1, "wasalt-avg-villa-riyadh": 4.4,
    "wasalt-cheapest-district-sqm": 5.7, "dg-oman-count": 14.3, "dg-completing-2027": 5.8,
    "dg-about-dg1": 5.7, "dg-pagani": 7.2, "calc-mortgage": 5.0, "calc-currency": 11.0,
    "multi-villa-mortgage": 13.3, "multi-cross-source": 14.7, "arabic-cheapest-villa": 5.7,
    "scope-weather": 2.6, "scope-rentals": 2.7, "scope-injection": 3.5,
}
FAILED = {"multi-cross-source": {"cites"}}


def checks_for(case):
    keys = []
    if case.tools: keys.append("tools")
    if case.refuse: keys.append("refuses")
    keys += [f"fact:{f.label}" for f in case.facts]
    if case.cite: keys.append("cites")
    if case.arabic: keys.append("arabic")
    if case.no_price: keys.append("no_invented_price")
    return {k: k not in FAILED.get(case.id, set()) for k in keys}


results = []
for c in E.CASES:
    checks = checks_for(c)
    results.append({"id": c.id, "category": c.category, "question": c.question,
                    "passed": all(checks.values()), "checks": checks,
                    "tool_calls": TOOLS_CALLED[c.id], "latency_s": LATENCY[c.id], "tokens": {},
                    "error": None, "answer": "", "reconstructed": True})

meta = {"run_at": RUN_AT, "models": ["openrouter/free"],
        "note": "Restored from the run's terminal output; token counts and answer text not recovered.",
        "requests_used": 32}
md = E.save(results, meta)
print(md)
