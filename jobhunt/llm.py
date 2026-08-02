"""The one place a model is called.

`complete(task, prompt)` is the whole interface. Which model runs a task lives in
`config/models.yaml` and nowhere else — never a model ID at a call site — so
swapping tiers, or adding an Ollama backend later, is a config change.

Every call writes an `llm_calls` row: prompt, response, token counts, cost, and
latency, on success and on failure both. Cost is reconstructed from the token
counts and the rates in models.yaml, because the API does not return a price.

Prompt caching is the first cost lever, not the last: the profile corpus is
byte-identical across every call in a month. Anything passed as `cached` becomes
a cache-control'd system block.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import yaml

from . import config
from .db import transaction

# A cache read bills at 10% of base input; a cache write at 125%.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

class LLMError(RuntimeError):
    """The model call failed, or the routing config is wrong."""


_CONFIG: dict[str, Any] | None = None


def routing() -> dict[str, Any]:
    global _CONFIG
    if _CONFIG is None:
        if not config.MODELS_YAML.exists():
            raise LLMError(f"{config.MODELS_YAML} is missing")
        _CONFIG = yaml.safe_load(config.MODELS_YAML.read_text()) or {}
    return _CONFIG


def task_config(task: str) -> dict[str, Any]:
    tasks = routing().get("tasks") or {}
    if task not in tasks:
        raise LLMError(
            f"no routing for task {task!r} in {config.MODELS_YAML.name}. "
            f"Known tasks: {sorted(tasks)}"
        )
    return tasks[task]


def rate(model: str) -> tuple[float, float]:
    """`(input, output)` $/million for `model`, from models.yaml. Raises if absent.

    Refusing is the point. Returning a zero rate for an unrecognised model — the
    old behaviour — meant swapping a model in models.yaml, which is exactly the
    change that file exists to make, silently logged every call at $0.00 and the
    spend vanished from `llm_calls` with nothing to notice.
    """
    table = routing().get("rates") or {}
    entry = table.get(model)
    if not entry:
        raise LLMError(
            f"no rate for model {model!r} in {config.MODELS_YAML.name}. Add it under "
            f"`rates:` — cost for every call using it would otherwise be logged as "
            f"$0.00. Known: {sorted(table)}"
        )
    intro = entry.get("intro") or {}
    # Introductory pricing lapses on its own date rather than needing an edit.
    if intro and str(intro.get("through", "")) >= config.today():
        return float(intro["input"]), float(intro["output"])
    return float(entry["input"]), float(entry["output"])


def _cost(model: str, usage: Any) -> float:
    rate_in, rate_out = rate(model)
    plain = getattr(usage, "input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        plain * rate_in
        + cache_read * rate_in * CACHE_READ_MULTIPLIER
        + cache_write * rate_in * CACHE_WRITE_MULTIPLIER
        + output * rate_out
    ) / 1_000_000


def _log(
    conn: sqlite3.Connection | None,
    *,
    task: str,
    model: str,
    prompt: str,
    application_id: int | None,
    response: str | None = None,
    usage: Any = None,
    latency_ms: int | None = None,
    error: str | None = None,
    stop_reason: str | None = None,
) -> None:
    if conn is None:
        return
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO llm_calls (
              task, model, application_id, prompt, response,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              cost_usd, latency_ms, error, stop_reason, called_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task,
                model,
                application_id,
                prompt,
                response,
                # Total input, cached included. The API reports cache reads and
                # writes in separate counters, so storing only `input_tokens`
                # would log 665 for a call that actually sent the whole corpus.
                (
                    (getattr(usage, "input_tokens", 0) or 0)
                    + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                    + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                    if usage
                    else None
                ),
                (getattr(usage, "output_tokens", None) if usage else None),
                # Kept separately as well as in the sum above: the split is the
                # only way to see whether caching is actually being read back.
                (getattr(usage, "cache_read_input_tokens", None) if usage else None),
                (getattr(usage, "cache_creation_input_tokens", None) if usage else None),
                (_cost(model, usage) if usage else None),
                latency_ms,
                error,
                stop_reason,
                config.utcnow(),
            ),
        )


def complete(
    task: str,
    prompt: str,
    *,
    conn: sqlite3.Connection | None = None,
    system: str | None = None,
    cached: str | None = None,
    application_id: int | None = None,
    expect_repeat: bool = True,
) -> str:
    """Run `task`'s model over `prompt` and return the text.

    `cached` is the byte-identical prefix — the profile corpus — sent as a
    cache-control'd system block so repeat calls bill it at a tenth.

    `expect_repeat` is the caller's answer to "will another call for this task
    follow within the 5-minute cache TTL?". Writing a cache entry costs 125% of
    base input and only pays back on a read, so a lone call that writes one is
    strictly worse off than a call that doesn't. Batch workers leave this True;
    the dashboard's single-JD path sets it False. See config/models.yaml.
    """
    settings = task_config(task)
    model = settings.get("model")
    if not model:
        raise LLMError(f"task {task!r} has no `model` in {config.MODELS_YAML.name}")
    rate(model)  # fail before spending money, not after logging the spend as $0

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise LLMError(
            "the anthropic package is not installed. "
            "Install it with: uv pip install -e '.[llm]'"
        ) from exc

    blocks: list[dict[str, Any]] = []
    if cached and settings.get("cache_profile", False) and expect_repeat:
        blocks.append(
            {"type": "text", "text": cached, "cache_control": {"type": "ephemeral"}}
        )
    elif cached:
        blocks.append({"type": "text", "text": cached})
    if system:
        blocks.append({"type": "text", "text": system})

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    started = time.monotonic()
    try:
        message = client.messages.create(
            model=model,
            max_tokens=int(settings.get("max_tokens", 2000)),
            system=blocks or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _log(
            conn,
            task=task,
            model=model,
            prompt=prompt,
            application_id=application_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise LLMError(f"{task} call failed: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    text = "".join(part.text for part in message.content if part.type == "text")
    # Log before raising: a truncated or refused reply is still the record of
    # what the call cost, and `llm_calls` is where that question gets answered.
    _log(
        conn,
        task=task,
        model=model,
        prompt=prompt,
        application_id=application_id,
        response=text,
        usage=message.usage,
        latency_ms=latency_ms,
        stop_reason=message.stop_reason,
    )

    # Without this, a truncated reply reaches the caller as a half-written string
    # and surfaces three layers up as "model reply is not valid JSON" — which
    # sends you to the prompt when the actual problem is the token budget.
    if message.stop_reason == "max_tokens":
        raise LLMError(
            f"{task} hit its {settings.get('max_tokens', 2000)}-token budget and the "
            f"reply is truncated. Raise `max_tokens` for {task!r} in "
            f"{config.MODELS_YAML.name}, or ask for less in one call. "
            "Note the budget covers reasoning as well as the reply."
        )
    if message.stop_reason == "refusal":
        raise LLMError(
            f"{task} was declined by the model's safety classifiers. "
            f"Nothing usable was returned."
        )
    return text
