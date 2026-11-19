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
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
import sys
from datetime import date

from dotenv import load_dotenv
from pydantic_ai import Agent, ModelRetry
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
   Questions that span both sites -> call tools for each, then compare.
2. Any arithmetic (price per sqm, differences, %, payments, currency) -> calculate,
   mortgage_payment or convert_gulf_currency. Never do maths in your head.
3. Ground every fact in tool results. If the tools return nothing relevant, say so plainly.
   Never invent listings, prices, dates or amenities.
4. Prices: Wasalt prices come from the listing. DarGlobal rarely publishes unit prices:
   say "price on request" unless a tool result clearly states a unit price for that project.
5. Cite sources: link each listing or project as a markdown link [title](url).
6. Scope: only property questions about this data. For other cities, rentals or topics outside
   the data, say what the data covers instead of guessing. Politely decline unrelated requests.
7. Reply in the user's language (Arabic or English). Be concise: one intro line, then a compact
   list or table of at most 8 rows, then one line of caveats if needed (sample data, scraped date).
   Keep answers under about 250 words."""


agent = Agent(build_model(), instructions=instructions, retries=2)


# ------------------------------------------------------------------ tools
@agent.tool_plain
def search_wasalt_listings(
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
    return tools.search_wasalt_listings(city, property_type, district, min_price, max_price,
                                        min_bedrooms, max_bedrooms, min_area_sqm, max_area_sqm,
                                        sort_by, limit)


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


@agent.tool_plain
def search_darglobal_projects(
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
    return tools.search_darglobal_projects(country, city, project_type, completion_year_from,
                                           completion_year_to, status, bedrooms, name_contains)


@agent.tool_plain
def search_knowledge(query: str, source: str | None = None, city: str | None = None,
                     limit: int = 6) -> dict:
    """Hybrid semantic + keyword search over DarGlobal project pages, location guides and
    Wasalt listing summaries. Use for descriptions, amenities, design partners, views,
    locations and anything not covered by exact filters.

    Args:
        query: what to look for, in natural language (English or Arabic).
        source: "darglobal" or "wasalt" to restrict the search.
        city: restrict to a city, e.g. "Jeddah".
        limit: number of passages (max 10).
    """
    if source not in (None, "darglobal", "wasalt"):
        raise ModelRetry('source must be "darglobal", "wasalt" or omitted')
    return tools.search_knowledge(query, source, city, min(limit, 10))


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
    history = []
    if len(sys.argv) > 1:
        result = await agent.run(" ".join(sys.argv[1:]))
        print(result.output)
        return
    print(f"Models: {MODELS}. Type 'exit' to quit.")
    while True:
        q = input("\nYou: ").strip()
        if q.lower() in {"exit", "quit"}:
            break
        result = await agent.run(q, message_history=history)
        history = result.all_messages()
        print(f"\nAgent: {result.output}")


if __name__ == "__main__":
    asyncio.run(_chat())
