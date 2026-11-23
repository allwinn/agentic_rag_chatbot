"""
Real-estate agent: PydanticAI + OpenRouter (free models) + typed tools over SQLite and LanceDB.

Env vars (.env):
  OPENROUTER_API_KEY   required
  OPENROUTER_MODELS    comma-separated, tried in order by OpenRouter itself
                       (default: openrouter/free, which picks a free model that supports tool calling)

CLI test:  python agent.py                       -> interactive chat in the terminal
           python agent.py "3 bed villas in Riyadh under 3M"
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
import sys
from datetime import date

from dotenv import load_dotenv
from dataclasses import replace as dc_replace

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

import tools

load_dotenv()

MODELS = [m.strip() for m in os.getenv("OPENROUTER_MODELS", "openrouter/free").split(",") if m.strip()]


def build_model():
    provider = OpenRouterProvider(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        app_title="DarGlobal x Wasalt Property Agent",
    )
    effort = os.getenv("REASONING_EFFORT", "low").lower()
    settings = OpenRouterModelSettings(
        temperature=0.2,
        # Free routing often lands on reasoning models, whose hidden "thinking" counts
        # against this budget, so leave plenty of room for the visible answer.
        max_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "4000")),
        openrouter_models=MODELS[1:] or None,  # server-side fallback if the first model is down/rate-limited
        # Keep reasoning short and don't send it back (ignored by non-reasoning models)
        openrouter_reasoning=None if effort == "off" else {"effort": effort, "exclude": True},
    )
    return OpenRouterModel(MODELS[0], provider=provider, settings=settings)


def instructions() -> str:
    s = tools.data_scope()
    return f"""You are a real-estate research assistant for two sources:
- DarGlobal: an international luxury developer. {s.get('darglobal_projects')} projects in {', '.join(s['darglobal_countries'])}.
- Wasalt: a Saudi property portal. {s.get('wasalt_listings')} listings for sale, cities: {', '.join(s['wasalt_cities'])}; types: {', '.join(s['wasalt_types'])}.
Data was scraped on {s.get('scraped_at') or 'a recent date'}. Today is {date.today().isoformat()}.

How to work:
1. Pick the right tool. Exact filters, prices, counts or averages -> SQL tools
   (search_wasalt_listings, wasalt_market_stats, search_darglobal_projects).
   Descriptions, amenities, design, location details, "tell me about X" -> search_knowledge.
   Don't call search_knowledge when the SQL tools already answer the question (lists, counts, prices).
   Questions that span both sites -> call tools for each, then compare.
   Contact or enquiry requests ("how do I contact", "call", "book a viewing") -> get_contact_info.
   Never invent phone numbers or emails, and never share private sellers' or brokers' details.
2. Any arithmetic (price per sqm, differences, %, payments, currency) -> calculate,
   mortgage_payment or convert_gulf_currency. Never do maths in your head.
3. Ground every fact in tool results. If the tools return nothing relevant, say so plainly.
   Never invent listings, prices, dates or amenities.
4. Prices: Wasalt prices come from the listing. DarGlobal rarely publishes unit prices:
   say "price on request" unless a tool result clearly states a unit price for that project.
5. Cite sources: link each listing or project as a markdown link [title](url), using the url from
   the tool result. This also applies to comparisons and long answers: if links don't fit in the
   text, end with a line "Sources:" listing them.
6. Scope: only property questions about this data. For other cities, rentals or topics outside
   the data, say what the data covers instead of guessing. Politely decline unrelated requests.
7. Reply in the user's language (Arabic or English). Be concise: one intro line, then a compact
   list or table of at most 8 rows, then one line of caveats if needed (sample data, scraped date).
   Keep answers under about 250 words.
8. Follow-ups: end every reply with a line containing exactly <<<FOLLOWUPS>>> followed by three
   short follow-up questions the user might ask next (max 10 words each, in the user's language),
   separated by " | ". Make them specific to this answer and answerable from the data, e.g. a
   narrower filter, a comparison with the other city or source, or a calculation on a result."""


FOLLOWUP_MARKER = "<<<FOLLOWUPS>>>"


def split_followups(text: str) -> tuple[str, list[str]]:
    """Separate the answer from the follow-up questions the model appends after the marker."""
    if FOLLOWUP_MARKER not in text:
        return text.strip(), []
    answer, _, tail = text.partition(FOLLOWUP_MARKER)
    parts = [p for chunk in tail.split("|") for p in chunk.split("\n")]
    qs = []
    for p in parts:
        q = p.strip().lstrip("-*•0123456789.) ").strip().strip('"').strip()
        if 4 <= len(q) <= 120 and q not in qs:
            qs.append(q)
    return answer.strip(), qs[:3]


@dataclass
class Deps:
    """Per-request state. `shown` collects the listings/projects the tools returned, with
    image and coordinates, so the UI can render cards and a map without sending those
    fields to the model (saves tokens)."""
    shown: list[dict] = field(default_factory=list)

    def capture(self, rows: list[dict], source: str) -> None:
        seen = {r["url"] for r in self.shown}
        for r in rows:
            if r.get("url") and r["url"] not in seen:
                self.shown.append({**r, "source": source})
                seen.add(r["url"])


# ------------------------------------------------------------------ history trimming
# Every model request resends the whole conversation. Old tool results (passages, listing rows)
# are the bulk of it and are rarely needed again, because the agent's written answers already
# carry the key facts. So: the last KEEP_FULL_TURNS questions keep everything; older tool results
# are replaced by a one-line summary. Results are shortened, never removed, so every tool call
# still has its matching result (APIs reject a call without one).
KEEP_FULL_TURNS = int(os.getenv("KEEP_FULL_TURNS", "2"))
_TRIMMED_NOTE = "Older result shortened to save tokens. Call the tool again if you need its details."


def _summarise_result(tool: str, content):
    if not isinstance(content, dict) or content.get("trimmed"):
        return content  # already short, or already summarised
    if "results" in content:  # search_knowledge
        titles = "; ".join(r.get("title", "")[:60] for r in content["results"])
        summary = f"{len(content['results'])} passages: {titles}"
    elif "listings" in content:  # search_wasalt_listings: keep what follow-ups refer to
        rows = [f"{r.get('title')} | {r.get('district')}, {r.get('city')} | "
                f"{r.get('currency', 'SAR')} {r.get('price'):,.0f} | {r.get('url')}"
                if isinstance(r.get("price"), (int, float)) else f"{r.get('title')} | {r.get('url')}"
                for r in content["listings"]]
        summary = f"{content.get('total_matching')} matching; shown: " + " || ".join(rows)
    elif "projects" in content:  # search_darglobal_projects
        summary = f"{content.get('count')} projects: " + "; ".join(
            f"{r.get('title')} ({r.get('url')})" for r in content["projects"][:12])
    else:  # stats, calculators, contacts: already small
        text = json.dumps(content, ensure_ascii=False, default=str)
        if len(text) <= 400:
            return content
        summary = text[:400] + "…"
    return {"trimmed": True, "tool": tool, "summary": summary, "note": _TRIMMED_NOTE}


def trim_old_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    question_starts = [i for i, m in enumerate(messages)
                       if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)]
    if len(question_starts) <= KEEP_FULL_TURNS:
        return messages
    cutoff = question_starts[-KEEP_FULL_TURNS]
    out: list[ModelMessage] = []
    for i, m in enumerate(messages):
        if i < cutoff and isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts):
            m = dc_replace(m, parts=[
                dc_replace(p, content=_summarise_result(p.tool_name, p.content))
                if isinstance(p, ToolReturnPart) else p for p in m.parts])
        out.append(m)
    return out


def _history_kwargs() -> dict:
    """pydantic-ai 2.x registers processors as a capability; 1.x took history_processors=."""
    try:
        from pydantic_ai.capabilities import ProcessHistory
        return {"capabilities": [ProcessHistory(trim_old_tool_results)]}
    except ImportError:
        return {"history_processors": [trim_old_tool_results]}


agent = Agent(build_model(), instructions=instructions, deps_type=Deps, retries=2, **_history_kwargs())


# ------------------------------------------------------------------ tools
@agent.tool
def search_wasalt_listings(
    ctx: RunContext[Deps],
    city: str | None = None,
    property_type: str | None = None,
    district: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = None,
    min_area_sqm: float | None = None,
    max_area_sqm: float | None = None,
    sort_by: tools.SortBy = "price_asc",
    limit: int = 8,
) -> dict:
    """Find Wasalt listings for sale in Saudi Arabia with exact filters.

    Args:
        city: e.g. "Riyadh" or "Jeddah".
        property_type: e.g. "Villa" or "Apartment".
        district: neighbourhood name, e.g. "Al-Malqa".
        min_price: minimum price in SAR.
        max_price: maximum price in SAR (e.g. 3 million = 3000000).
        min_bedrooms: minimum bedrooms.
        max_bedrooms: maximum bedrooms.
        min_area_sqm: minimum built-up (or land) area in square metres.
        max_area_sqm: maximum area in square metres.
        sort_by: price_asc, price_desc, price_per_sqm_asc, area_desc or newest.
        limit: rows to return (max 15).
    """
    res = tools.search_wasalt_listings(city, property_type, district, min_price, max_price,
                                       min_bedrooms, max_bedrooms, min_area_sqm, max_area_sqm,
                                       sort_by, limit)
    ctx.deps.capture(res["listings"], "wasalt")
    return {**res, "listings": tools.strip_ui_fields(res["listings"])}


@agent.tool_plain
def wasalt_market_stats(
    city: str | None = None,
    property_type: str | None = None,
    district: str | None = None,
    bedrooms: int | None = None,
    group_by_district: bool = False,
    sort_districts_by: tools.DistrictSort = "listings",
    top_n: int = 8,
) -> dict:
    """Price statistics for Wasalt listings: count, average, min, max and average price per sqm (SAR).

    Args:
        city: e.g. "Riyadh".
        property_type: e.g. "Villa" or "Apartment".
        district: limit to one district.
        bedrooms: exact bedroom count.
        group_by_district: true to compare districts side by side.
        sort_districts_by: when grouping: listings, price_per_sqm_asc, price_per_sqm_desc,
            avg_price_asc or avg_price_desc (e.g. "cheapest per sqm" -> price_per_sqm_asc).
        top_n: number of districts to return when grouping (max 15).
    """
    return tools.wasalt_market_stats(city, property_type, district, bedrooms, group_by_district,
                                     sort_districts_by, top_n)


@agent.tool
def search_darglobal_projects(
    ctx: RunContext[Deps],
    country: str | None = None,
    city: str | None = None,
    project_type: str | None = None,
    completion_year_from: int | None = None,
    completion_year_to: int | None = None,
    status: str | None = None,
    bedrooms: int | None = None,
    name_contains: str | None = None,
) -> dict:
    """List DarGlobal projects with exact filters. Call with no arguments to list all projects.

    Args:
        country: UAE, Saudi Arabia, Oman, Qatar, Spain or UK.
        city: e.g. "Dubai", "Jeddah", "Muscat".
        project_type: "Apartments", "Villas" or "Hotel Rooms".
        completion_year_from: earliest completion year.
        completion_year_to: latest completion year.
        status: Off-plan, Sold out, Completed, Completion date passed or Unknown.
        bedrooms: projects offering this bedroom count.
        name_contains: part of the project name, e.g. "Trump".
    """
    if status not in (None, "Off-plan", "Sold out", "Completed", "Completion date passed", "Unknown"):
        raise ModelRetry("status must be one of: Off-plan, Sold out, Completed, Completion date passed, Unknown")
    res = tools.search_darglobal_projects(country, city, project_type, completion_year_from,
                                          completion_year_to, status, bedrooms, name_contains)
    if res["count"] <= 12:  # don't flood the UI when the agent lists everything
        ctx.deps.capture(res["projects"], "darglobal")
    return {**res, "projects": tools.strip_ui_fields(res["projects"])}


@agent.tool_plain
def search_knowledge(query: str, source: str | None = None, city: str | None = None,
                     limit: int = 4) -> dict:
    """Hybrid semantic + keyword search over DarGlobal project pages, location guides and
    Wasalt listing summaries. Use for descriptions, amenities, design partners, views,
    locations and anything not covered by exact filters.

    Args:
        query: what to look for, in natural language (English or Arabic).
        source: "darglobal" or "wasalt" to restrict the search.
        city: restrict to a city, e.g. "Jeddah".
        limit: number of passages (default 4, max 8). Each is one project or document.
    """
    if source not in (None, "darglobal", "wasalt"):
        raise ModelRetry('source must be "darglobal", "wasalt" or omitted')
    return tools.search_knowledge(query, source, city, max(1, min(limit, 8)))


@agent.tool_plain
def get_contact_info(source: str, listing_url: str | None = None, project_name: str | None = None) -> dict:
    """How to contact DarGlobal or a Wasalt advertiser. Use for any contact, enquiry or viewing request.

    Args:
        source: "darglobal" for the developer's official contacts, "wasalt" for a listing's advertiser.
        listing_url: for Wasalt, the listing's url from a previous search result.
        project_name: for DarGlobal, the project the user is interested in, e.g. "DG1".
    """
    if source not in ("darglobal", "wasalt"):
        raise ModelRetry('source must be "darglobal" or "wasalt"')
    return tools.get_contact_info(source, listing_url, project_name)


@agent.tool_plain
def calculate(expression: str) -> dict:
    """Evaluate arithmetic exactly, e.g. "3200000 / 450" or "(2.1e6 - 1.8e6) / 1.8e6 * 100".

    Args:
        expression: numbers with + - * / ** % and parentheses.
    """
    return tools.calculate(expression)


@agent.tool_plain
def mortgage_payment(price: float, down_payment_pct: float = 20, annual_rate_pct: float = 5.5,
                     years: int = 25) -> dict:
    """Estimate a monthly mortgage payment (illustrative, not a bank offer).

    Args:
        price: property price.
        down_payment_pct: down payment as % of price.
        annual_rate_pct: annual interest rate in %.
        years: loan term in years.
    """
    return tools.mortgage_payment(price, down_payment_pct, annual_rate_pct, years)


@agent.tool_plain
def convert_gulf_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert between USD-pegged currencies: USD, SAR, AED, QAR, OMR, BHD.

    Args:
        amount: amount to convert.
        from_currency: e.g. "SAR".
        to_currency: e.g. "AED".
    """
    return tools.convert_gulf_currency(amount, from_currency, to_currency)


# ------------------------------------------------------------------ CLI
async def _chat() -> None:
    import observability
    observability.setup()
    try:
        await _chat_loop()
    finally:
        observability.flush()


async def _chat_loop() -> None:
    history = []
    if len(sys.argv) > 1:
        result = await agent.run(" ".join(sys.argv[1:]), deps=Deps())
        answer, followups = split_followups(result.output)
        print(answer + ("\n\nFollow-ups: " + " | ".join(followups) if followups else ""))
        return
    print(f"Models: {MODELS}. Type 'exit' to quit.")
    while True:
        q = input("\nYou: ").strip()
        if q.lower() in {"exit", "quit"}:
            break
        result = await agent.run(q, message_history=history, deps=Deps())
        history = result.all_messages()
        answer, followups = split_followups(result.output)
        print(f"\nAgent: {answer}" + ("\n\nFollow-ups: " + " | ".join(followups) if followups else ""))


if __name__ == "__main__":
    asyncio.run(_chat())
