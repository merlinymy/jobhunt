"""The one place a model is called.

`complete(task, prompt)` is the whole interface. Which model runs a task lives in
`config/models.yaml` and nowhere else — never a model ID at a call site — so
swapping tiers, or adding an Ollama backend later, is a config change.

The same holds for the instructions: a task's system prompt is
`config/prompts/<task>.md`, not a constant in the worker. Prompt wording is the
thing most worth iterating on and the thing least worth a code change to edit.

Every call writes an `llm_calls` row: prompt, response, token counts, cost, and
latency, on success and on failure both. Cost is reconstructed from the token
counts and the rates in models.yaml, because the API does not return a price.

Prompt caching is the first cost lever, not the last: the profile corpus is
byte-identical across every call in a month. Anything passed as `cached` becomes
a cache-control'd system block.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
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


def prompt_path(task: str) -> Path:
    """Where `task`'s system prompt lives. `config/prompts/<task>.md` by default.

    `tasks.<task>.system_prompt` in models.yaml overrides it, relative to
    `config/`, so two tasks can share one file without a copy going stale.
    """
    override = task_config(task).get("system_prompt")
    if override:
        return config.MODELS_YAML.parent / str(override)
    return config.PROMPTS_DIR / f"{task}.md"


def system_prompt(task: str, conn: sqlite3.Connection | None = None) -> str:
    """`task`'s system prompt, read fresh.

    Deliberately not memoized. Prompt wording is what you iterate on, and a
    cached copy would mean restarting the dashboard between edits — one 4 KB
    read against a call that takes seconds is not a cost worth optimizing.

    An active row in `prompts` wins over the file when a connection is given.
    That is the dashboard editor; the file is the git-tracked default and what
    every caller gets back the moment the override is reverted. Passing no
    connection reads the file, which is what keeps the prompt loader usable from
    places that have no database — `python -m jobhunt.llm` among them.
    """
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT body FROM prompts WHERE task = ? AND active = 1", (task,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Pre-012 database. The file is the answer, and it always was.
            row = None
        if row and str(row["body"]).strip():
            return str(row["body"]).strip()

    path = prompt_path(task)
    if not path.exists():
        raise LLMError(
            f"no system prompt for task {task!r}: expected {path}. Create it, or "
            f"point `tasks.{task}.system_prompt` in {config.MODELS_YAML.name} at "
            f"an existing file. See config/prompts/README.md."
        )
    text = path.read_text().strip()
    if not text:
        # An empty system block is accepted by the API and quietly produces
        # markedly worse output — the kind of regression that gets blamed on the
        # model. Refuse instead: a truncated save is the likeliest cause.
        raise LLMError(f"{path} is empty. A blank system prompt is never intended.")
    return text


def prompt_sha(text: str) -> str:
    """Short content hash, logged so output can be traced to a prompt revision.

    Prompts are editable and git-tracked; this is the join between a row in
    `llm_calls` and the wording that produced it. Twelve hex chars is ample for
    telling a handful of revisions apart.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:12]


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
    system_sha: str | None = None,
) -> None:
    if conn is None:
        return
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO llm_calls (
              task, model, application_id, prompt, response,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              cost_usd, latency_ms, error, stop_reason, system_sha, called_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                system_sha,
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
    history: list[dict[str, str]] | None = None,
) -> str:
    """Run `task`'s model over `prompt` and return the text.

    `system` defaults to `config/prompts/<task>.md`. Pass it only to override
    that file for one call; workers should not, or the file stops being the
    place a prompt is edited.

    `history` is prior turns — `[{"role": "user"|"assistant", "content": ...}]` —
    placed before `prompt`, which is always the last user message. Everything is
    still one `complete(task, prompt)` call; this is the difference between
    continuing a conversation and describing one. The revision chat replays the
    build's own exchange so the model sees the resume it wrote and the reasoning
    it wrote it with, rather than a summary of both written by someone else. It
    also makes the cache prefix longer, since those turns are byte-identical on
    every subsequent turn.

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
    system = system if system is not None else system_prompt(task, conn)
    sha = prompt_sha(system)

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

    turns: list[dict[str, Any]] = []
    for turn in history or ():
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        # A malformed turn would be rejected by the API with a 400 that says
        # nothing about which turn; drop it here instead. Alternation is not
        # enforced — the API tolerates consecutive same-role turns by merging.
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    turns.append({"role": "user", "content": prompt})

    # Reasoning config from models.yaml; a per-task key overrides the global one.
    # Opus 4.8 takes adaptive thinking only — `budget_tokens` is rejected with a
    # 400 — and effort sets the depth (`max` reasons hardest). Thinking arrives as
    # separate content parts, so the `type == "text"` extraction below is already
    # correct; `max_tokens` covers thinking *and* the reply (see models.yaml).
    reasoning: dict[str, Any] = {}
    think = settings.get("thinking", routing().get("thinking"))
    if think and str(think).lower() != "off":
        reasoning["thinking"] = {"type": "adaptive"}
    effort = settings.get("effort", routing().get("effort"))
    if effort:
        reasoning["output_config"] = {"effort": str(effort)}

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    started = time.monotonic()
    try:
        # Streamed, not a plain create: with max-effort thinking the budget covers
        # reasoning as well as the reply, so max_tokens runs large (form answers
        # need ~24k), and the SDK refuses a non-streaming request it estimates will
        # exceed ~10 minutes. get_final_message() returns the same Message a create
        # would, so nothing downstream changes.
        with client.messages.stream(
            model=model,
            max_tokens=int(settings.get("max_tokens", 2000)),
            system=blocks or anthropic.NOT_GIVEN,
            messages=turns,
            **reasoning,
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:
        _log(
            conn,
            task=task,
            model=model,
            prompt=prompt,
            application_id=application_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
            system_sha=sha,
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
        system_sha=sha,
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


def main() -> None:
    """`python -m jobhunt.llm` — what every task is routed to right now.

    The shas are the ones written to `llm_calls.system_sha`, so a row from last
    week can be matched to the wording that produced it.
    """
    for task in sorted(routing().get("tasks") or {}):
        settings = task_config(task)
        path = prompt_path(task)
        try:
            sha = prompt_sha(system_prompt(task))
        except LLMError:
            sha = "-- missing --"
        root = config.REPO_ROOT
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        print(f"{task:<16} {settings.get('model', '?'):<28} {sha:<14} {rel}")


if __name__ == "__main__":
    main()
