"""
Chainlit chat UI for the property agent.

- Streams the answer token by token
- Shows every tool call as an expandable step (arguments in, result out), so reviewers
  can see the agent choose between SQL search, hybrid search and calculators
- Keeps conversation history per browser session
- Shows property cards + a map for listings, and 3 clickable follow-up questions per answer
- Caps requests per answer and messages per session (protects the OpenRouter key on a public URL)

Run locally:  chainlit run app.py -w
"""
from __future__ import annotations

import json
import os

import chainlit as cl
from pydantic_ai.exceptions import ModelHTTPError, OutputTokenLimitExceeded, UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.usage import UsageLimits

from agent import FOLLOWUP_MARKER, MODELS, Deps, agent, split_followups
import tools

MAX_CARDS = 6

MAX_MESSAGES_PER_SESSION = int(os.getenv("MAX_MESSAGES_PER_SESSION", "30"))
LIMITS = UsageLimits(request_limit=8)  # max model calls per answer: stops tool-call loops


def _money(v, cur="SAR") -> str:
    return f"{cur} {v:,.0f}" if isinstance(v, (int, float)) else "Price on request"


def _card(r: dict) -> dict:
    """Shape a captured tool row into what PropertyResults.jsx renders."""
    if r["source"] == "wasalt":
        area = r.get("built_up_area_sqm") or r.get("land_area_sqm")
        meta = " · ".join(x for x in [
            f"{r['bedrooms']} bd" if r.get("bedrooms") else "",
            f"{r['bathrooms']} ba" if r.get("bathrooms") else "",
            f"{area:,.0f} m²" if area else "",
            f"{_money(r['price_per_sqm'], '')}/m²".strip() if r.get("price_per_sqm") else "",
        ] if x)
        return {"source": "wasalt", "title": r.get("title") or "Listing", "url": r["url"],
                "price": _money(r.get("price"), r.get("currency") or "SAR"),
                "subtitle": f"{r.get('property_type') or ''} · {r.get('district') or ''}, {r.get('city') or ''}",
                "meta": meta, "image": r.get("image"), "lat": r.get("lat"), "lon": r.get("lon")}
    return {"source": "darglobal", "title": r.get("title") or "Project", "url": r["url"],
            "price": "Price on request",
            "subtitle": f"{r.get('project_type') or ''} · {r.get('location') or ''}",
            "meta": " · ".join(x for x in [r.get("bedrooms_text") or "",
                                           r.get("status") if r.get("status") not in (None, "Unknown") else "",
                                           f"Completion {r['completion']}" if r.get("completion") else ""] if x),
            "image": r.get("image"), "lat": None, "lon": None}


def _results_element(deps: Deps):
    if not deps.shown:
        return None
    cards = [_card(r) for r in deps.shown]
    return cl.CustomElement(name="PropertyResults", display="inline",
                            props={"items": cards[:MAX_CARDS], "total": len(cards)})


def _short(obj, n: int = 3000) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= n else text[:n] + "\n… (truncated)"


@cl.set_starters
async def starters():
    # Each starter needs 2+ tools, often across different data sources, so the steps show
    # the agent planning: SQL (DarGlobal / Wasalt), knowledge search and calculators.
    return [
        cl.Starter(
            label="Dubai budget, Saudi home",
            message=("I live in Dubai and have AED 5 million. What can I buy in Jeddah on Wasalt "
                     "with that budget, and which DarGlobal projects are in Jeddah?")),
        cl.Starter(
            label="Riyadh vs Jeddah villas",
            message=("Compare villas in Riyadh and Jeddah: average price, average price per sqm, "
                     "and which city gives more space for the money?")),
        cl.Starter(
            label="Luxury-brand residences",
            message=("Which DarGlobal projects are designed with luxury brands like Pagani, "
                     "Lamborghini, Aston Martin or Missoni? Where are they and when do they complete?")),
        cl.Starter(
            label="Cheapest villa + mortgage",
            message=("Find the cheapest 4-bedroom villa in Riyadh and estimate the monthly mortgage "
                     "with 25% down over 20 years.")),
        cl.Starter(
            label="Trump Tower Jeddah vs market",
            message=("Tell me about DarGlobal's Trump Tower Jeddah, then show what similar apartments "
                     "for sale in Jeddah cost on Wasalt.")),
        cl.Starter(
            label="بالعربي: الرياض أم جدة؟",
            message="قارن بين متوسط أسعار الشقق في الرياض وجدة، وما هي أرخص الأحياء للشقق في كل مدينة؟"),
    ]


@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])
    cl.user_session.set("count", 0)


class MarkerFilter:
    """Streams text but hides everything from the follow-up marker onwards. Holds back a few
    characters whenever the stream ends in what could be the start of the marker."""

    def __init__(self, marker: str = FOLLOWUP_MARKER):
        self.marker, self.buf, self.hidden = marker, "", False

    def feed(self, token: str) -> str:
        if self.hidden:
            return ""
        self.buf += token
        i = self.buf.find(self.marker)
        if i >= 0:
            out, self.buf, self.hidden = self.buf[:i], "", True
            return out
        keep = 0  # longest suffix of buf that is a prefix of the marker
        for k in range(min(len(self.marker) - 1, len(self.buf)), 0, -1):
            if self.marker.startswith(self.buf[-k:]):
                keep = k
                break
        out, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
        return out


# Used when a model forgets the marker: suggestions based on which tools it used
FALLBACK_FOLLOWUPS = {
    "search_wasalt_listings": ["Estimate the mortgage on the cheapest one",
                               "Show the same in the other city",
                               "Which district is cheapest per sqm?"],
    "wasalt_market_stats": ["Show me listings in the cheapest district",
                            "Compare this with the other city",
                            "Only 4-bedroom properties"],
    "search_darglobal_projects": ["Tell me more about the first project",
                                  "Which of these complete soonest?",
                                  "Compare with Wasalt listings in the same city"],
    "search_knowledge": ["Which DarGlobal projects are in Saudi Arabia?",
                         "What is the completion date?",
                         "Compare with homes for sale on Wasalt"],
    "mortgage_payment": ["What if I put 30% down?", "Over 15 years instead",
                         "Show other villas in this price range"],
}
FALLBACK_AR = ["أرني فللاً في جدة", "قارن الأسعار بين الرياض وجدة", "ما هي مشاريع دار جلوبال في السعودية؟"]


def _fallback_followups(tools_used: list[str], question: str) -> list[str]:
    if any("\u0600" <= c <= "\u06ff" for c in question):
        return FALLBACK_AR
    for t in tools_used:
        if t in FALLBACK_FOLLOWUPS:
            return FALLBACK_FOLLOWUPS[t]
    return ["Cheapest villas in Riyadh", "DarGlobal projects in Saudi Arabia",
            "Compare apartment prices in Riyadh and Jeddah"]


async def _clear_followups():
    """Remove the previous answer's follow-up buttons so only the latest set is clickable."""
    for a in cl.user_session.get("followup_actions", []) or []:
        try:
            await a.remove()
        except Exception:
            pass
    cl.user_session.set("followup_actions", [])


@cl.on_message
async def on_message(message: cl.Message):
    await respond(message.content)


@cl.action_callback("followup")
async def on_followup(action: cl.Action):
    question = (action.payload or {}).get("q", "").strip()
    if not question:
        return
    await _clear_followups()
    await cl.Message(content=question, author="User", type="user_message").send()
    await respond(question)


async def respond(question: str):
    count = cl.user_session.get("count", 0) + 1
    cl.user_session.set("count", count)
    await _clear_followups()
    if count > MAX_MESSAGES_PER_SESSION:
        await cl.Message(content="This demo session has reached its message limit. "
                                 "Please refresh the page to start a new one.").send()
        return

    history = cl.user_session.get("history", [])
    deps = Deps()
    marker = MarkerFilter()
    tools_used: list[str] = []
    # Created lazily at the first text token, so tool steps (created earlier) appear above it
    answer: cl.Message | None = None

    async def say(token: str):
        nonlocal answer
        visible = marker.feed(token)
        if not visible:
            return
        if answer is None:
            answer = cl.Message(content="")
        await answer.stream_token(visible)

    steps: dict[str, cl.Step] = {}
    followups: list[str] = []

    try:
        async with agent.run_stream_events(question, message_history=history,
                                           usage_limits=LIMITS, deps=deps) as run:
            async for event in run:
                if isinstance(event, FunctionToolCallEvent):
                    tools_used.append(event.part.tool_name)
                    # Same parent as the answer message, so steps render above the answer
                    run_step = cl.context.current_step
                    step = cl.Step(name=event.part.tool_name, type="tool", show_input="json",
                                   parent_id=run_step.id if run_step else None)
                    step.input = event.part.args_as_dict()
                    await step.send()
                    steps[event.part.tool_call_id] = step
                elif isinstance(event, FunctionToolResultEvent):
                    step = steps.get(event.tool_call_id)
                    if step:
                        # field is `part` in pydantic-ai 2.x (`result` in older 1.x releases)
                        returned = getattr(event, "part", None) or getattr(event, "result", None)
                        step.output = _short(getattr(returned, "content", returned))
                        step.language = "json"
                        await step.update()
                elif isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        await say(event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    await say(event.delta.content_delta)
            result = run.result
        text, followups = split_followups(str(result.output) if result is not None else "")
        if answer is None:
            answer = cl.Message(content=text)
        else:
            answer.content = text  # drop anything held back by the marker filter
        cl.user_session.set("history", run.all_messages())
        element = _results_element(deps)
        if element:
            answer.elements = [element]
        followups = followups or _fallback_followups(tools_used, question)
    except Exception as e:
        if answer is None:
            answer = cl.Message(content="")
        answer.content = _error_text(e)
        followups = []

    if followups:
        actions = [cl.Action(name="followup", payload={"q": q}, label=q, icon="corner-down-right")
                   for q in followups]
        answer.actions = actions
        cl.user_session.set("followup_actions", actions)
    await answer.send()


def _error_text(e: Exception) -> str:
    """Friendly message for each failure mode, so the UI never shows a raw stack trace."""
    if isinstance(e, OutputTokenLimitExceeded):
        return ("That answer ran too long for the model's output limit. Try narrowing it down, "
                "e.g. one city or property type, or ask for the top 5.")
    if isinstance(e, UsageLimitExceeded):
        return ("I needed too many steps for that one. Could you make the question a bit more "
                "specific (city, type, budget)?")
    if isinstance(e, ModelHTTPError):
        if e.status_code == 429:
            return "The free AI model is rate-limited right now. Please wait a few seconds and try again."
        return f"The AI model returned an error ({e.status_code}). Please try again."
    print(f"[error] {type(e).__name__}: {e}")
    return f"Something went wrong: {type(e).__name__}. Please try again."


# Warm up the embedding model and DB at startup so the first question is fast
try:
    tools.search_knowledge("warmup", limit=1)
    print(f"[startup] data ready; models: {MODELS}")
except Exception as e:  # pragma: no cover
    print(f"[startup] warmup failed: {e}")
