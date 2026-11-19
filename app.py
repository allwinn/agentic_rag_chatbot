"""
Chainlit chat UI for the property agent.

- Streams the answer token by token
- Shows every tool call as an expandable step (arguments in, result out), so reviewers
  can see the agent choose between SQL search, hybrid search and calculators
- Keeps conversation history per browser session
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

from agent import agent, MODELS
import tools

MAX_MESSAGES_PER_SESSION = int(os.getenv("MAX_MESSAGES_PER_SESSION", "30"))
LIMITS = UsageLimits(request_limit=8)  # max model calls per answer: stops tool-call loops


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


@cl.on_message
async def on_message(message: cl.Message):
    count = cl.user_session.get("count", 0) + 1
    cl.user_session.set("count", count)
    if count > MAX_MESSAGES_PER_SESSION:
        await cl.Message(content="This demo session has reached its message limit. "
                                 "Please refresh the page to start a new one.").send()
        return

    history = cl.user_session.get("history", [])
    answer = cl.Message(content="")
    steps: dict[str, cl.Step] = {}

    try:
        async with agent.run_stream_events(message.content, message_history=history,
                                           usage_limits=LIMITS) as run:
            async for event in run:
                if isinstance(event, FunctionToolCallEvent):
                    step = cl.Step(name=event.part.tool_name, type="tool", show_input="json")
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
                        await answer.stream_token(event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    await answer.stream_token(event.delta.content_delta)
            result = run.result
        if not answer.content.strip() and result is not None:
            answer.content = str(result.output)
        cl.user_session.set("history", run.all_messages())
    except OutputTokenLimitExceeded:
        answer.content = ("That answer ran too long for the model's output limit. Try narrowing it "
                          "down, e.g. one city or property type, or ask for the top 5.")
    except UsageLimitExceeded:
        answer.content = ("I needed too many steps for that one. Could you make the question "
                          "a bit more specific (city, type, budget)?")
    except ModelHTTPError as e:
        if e.status_code == 429:
            answer.content = ("The free AI model is rate-limited right now. "
                              "Please wait a few seconds and try again.")
        else:
            answer.content = f"The AI model returned an error ({e.status_code}). Please try again."
    except Exception as e:  # keep the UI alive whatever happens
        answer.content = f"Something went wrong: {type(e).__name__}. Please try again."
        print(f"[error] {type(e).__name__}: {e}")

    await answer.send()


# Warm up the embedding model and DB at startup so the first question is fast
try:
    tools.search_knowledge("warmup", limit=1)
    print(f"[startup] data ready; models: {MODELS}")
except Exception as e:  # pragma: no cover
    print(f"[startup] warmup failed: {e}")
