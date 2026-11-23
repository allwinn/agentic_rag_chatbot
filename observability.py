"""
Langfuse observability for the agent (optional: the app runs normally without it).

When LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set, every agent run is traced:
  - one trace per user question, named and grouped by chat session
  - inside it: each model call (prompt, response, model, input/output tokens, latency)
    and each tool call (arguments and result)
PydanticAI emits OpenTelemetry spans (Agent.instrument_all); the Langfuse SDK registers
itself as the OpenTelemetry exporter, so no extra wiring is needed.

Env vars:
  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
  LANGFUSE_BASE_URL     https://cloud.langfuse.com (EU, default) or https://us.cloud.langfuse.com
  LANGFUSE_TRACING_ENVIRONMENT   e.g. production / local (shown as a filter in Langfuse)
"""
from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

_client = None


def setup() -> bool:
    """Turn tracing on if Langfuse keys are configured. Never raises: tracing is optional."""
    global _client
    if _client is not None:
        return True
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        print("[langfuse] tracing off (no LANGFUSE_* keys)")
        return False
    try:
        from langfuse import get_client
        from pydantic_ai import Agent

        client = get_client()
        if not client.auth_check():
            print("[langfuse] auth failed: check the keys and LANGFUSE_BASE_URL; tracing off")
            return False
        Agent.instrument_all()  # every model + tool call becomes an OpenTelemetry span
        _client = client
        print(f"[langfuse] tracing on -> {os.getenv('LANGFUSE_BASE_URL') or os.getenv('LANGFUSE_HOST') or 'https://cloud.langfuse.com'}")
        return True
    except Exception as e:  # pragma: no cover
        print(f"[langfuse] could not start ({type(e).__name__}: {e}); tracing off")
        return False


def enabled() -> bool:
    return _client is not None


@contextmanager
def trace(name: str, *, session_id: str | None = None, user_id: str | None = None,
          tags: list[str] | None = None, metadata: dict[str, Any] | None = None,
          input: Any = None) -> Iterator[Any]:
    """Group everything inside into one named Langfuse trace. Yields the root span (or None
    when tracing is off), so callers can attach the final output or scores."""
    if _client is None:
        yield None
        return
    from langfuse import propagate_attributes

    with ExitStack() as stack:
        try:
            stack.enter_context(propagate_attributes(
                trace_name=name, session_id=session_id, user_id=user_id, tags=tags,
                metadata={k: str(v) for k, v in (metadata or {}).items()}, version=APP_VERSION))
            span = stack.enter_context(_client.start_as_current_observation(
                name=name, as_type="agent", input=input))
        except Exception as e:  # never let tracing break a user request
            print(f"[langfuse] trace start failed: {e}")
            span = None
        yield span


def finish(span: Any, output: Any = None, metadata: dict[str, Any] | None = None,
           error: str | None = None) -> None:
    if span is None:
        return
    try:
        span.update(output=output, metadata=metadata,
                    level="ERROR" if error else None, status_message=error)
    except Exception:
        pass


def score(span: Any, name: str, value: float, comment: str | None = None) -> None:
    """Attach a score to the whole trace (used by the eval suite: pass/fail per check)."""
    if span is None:
        return
    try:
        span.score_trace(name=name, value=value, comment=comment)
    except Exception:
        pass


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            pass
