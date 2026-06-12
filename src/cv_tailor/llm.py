"""Central LLM abstraction layer.

All agents call call_llm() — never litellm.completion directly.
Handles config loading, JSONL logging, error logging, and snippet hashing.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm
import yaml

from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

SNIPPET_LOG_MAX = 200   # chars considered for hashing only; raw snippets are never logged
CONFIG_PATH = Path("config.yaml")
PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _llm_log_path() -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    p = Path("logs") / month / "llm_calls.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _error_log_path() -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    p = Path("logs") / month / "errors.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_log_lock = threading.Lock()

# ---------------------------------------------------------------------------
# In-process caches (config + prompts) — valid for the lifetime of one process.
# These files never change during a run, so caching is safe.
#
# Dev-Mode: set CV_TAILOR_DEV_RELOAD=1 to invalidate cached prompts when
# their mtime advances. Saves a server restart on every prompt edit
# during iteration. Production-Mode (default) keeps the lifetime cache —
# zero stat() overhead per call.
# ---------------------------------------------------------------------------
_config_cache: dict[str, dict] = {}              # key: "<config_path>::<agent_name>"
_prompt_cache: dict[str, tuple[float, str]] = {}  # key: abs path → (mtime, content)


def load_prompt(path: Path) -> str:
    """Read and cache a prompt file. Raises FileNotFoundError if missing.

    Honours CV_TAILOR_DEV_RELOAD=1 by invalidating the cache when the
    file's mtime moves forward.
    """
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    key = str(path.resolve())
    dev_reload = os.environ.get("CV_TAILOR_DEV_RELOAD") == "1"
    if key in _prompt_cache and not dev_reload:
        return _prompt_cache[key][1]
    current_mtime = path.stat().st_mtime
    if key in _prompt_cache and _prompt_cache[key][0] >= current_mtime:
        return _prompt_cache[key][1]
    content = path.read_text(encoding="utf-8")
    _prompt_cache[key] = (current_mtime, content)
    return content


def _append_jsonl(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _log_lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_agent_config(agent_name: str, config_path: Path = CONFIG_PATH) -> dict:
    """Return {"provider": ..., "model": ...} for the named agent from config.yaml.

    Result is cached in-process — config.yaml never changes during a run, so
    the YAML parse cost (called 20+ times per run) is paid only once per agent.

    Raises:
        FileNotFoundError: If config.yaml is missing.
        ValueError: If agent_name not found or missing provider/model.
    """
    cache_key = f"{config_path}::{agent_name}"
    if cache_key in _config_cache:
        return _config_cache[cache_key]
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    agents = (cfg or {}).get("agents", {})
    agent_cfg = agents.get(agent_name)
    if not agent_cfg:
        raise ValueError(
            f"Agent '{agent_name}' not found in config.yaml. "
            f"Available agents: {list(agents.keys())}"
        )
    if "provider" not in agent_cfg or "model" not in agent_cfg:
        raise ValueError(
            f"config.yaml: agents.{agent_name} must have both 'provider' and 'model' keys. "
            f"Got: {list(agent_cfg.keys())}"
        )
    result = {"provider": agent_cfg["provider"], "model": agent_cfg["model"]}
    _config_cache[cache_key] = result
    return result


def validate_llm_environment(config_path: Path = CONFIG_PATH) -> list[str]:
    """Return human-readable configuration problems for required LLM providers."""
    if not config_path.exists():
        return [f"config.yaml nicht gefunden: {config_path}"]

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    agents = cfg.get("agents", {})
    providers = {
        str(agent_cfg.get("provider", "")).strip().lower()
        for agent_cfg in agents.values()
        if isinstance(agent_cfg, dict)
    }

    problems: list[str] = []
    for provider in sorted(providers):
        env_var = PROVIDER_ENV_VARS.get(provider)
        if env_var is None:
            log.warning("validate_llm_environment.unknown_provider", provider=provider)
            continue
        value = os.getenv(env_var, "").strip()
        if not value or "REPLACE_ME" in value or value.endswith("_ME"):
            problems.append(f"{env_var} fehlt oder enthält noch einen Platzhalter")
    return problems


def require_llm_environment(config_path: Path = CONFIG_PATH) -> None:
    """Raise RuntimeError if required LLM API keys are missing or placeholders."""
    problems = validate_llm_environment(config_path)
    if problems:
        details = "; ".join(problems)
        raise RuntimeError(
            "LLM-Konfiguration unvollständig. Bitte .env aus .env.example erstellen "
            f"und echte API-Keys setzen: {details}"
        )


# ---------------------------------------------------------------------------
# Message preparation
# ---------------------------------------------------------------------------

def _prepare_messages_for_provider(messages: list[dict], provider: str) -> list[dict]:
    """Adapt message format to the target provider.

    Anthropic — prompt caching:
      - String system prompts are wrapped in a content array with
        ``cache_control: {type: ephemeral}`` so Anthropic caches the system
        prompt across calls that share the same prefix.
      - User messages that are already content arrays (list of dicts) are left
        as-is; callers may include ``cache_control`` blocks for large static
        context (e.g. beleg_index, analyse) to extend the cache prefix.

    Other providers (OpenAI etc.) — flatten:
      - Content arrays are flattened back to plain strings. ``cache_control``
        is stripped because those providers reject unknown keys.
    """
    result: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if provider == "anthropic":
            if msg.get("role") == "system" and isinstance(content, str):
                msg = {
                    **msg,
                    "content": [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ],
                }
        else:
            # Flatten list-of-dicts content to a plain string for non-Anthropic providers.
            if isinstance(content, list):
                text = "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                msg = {**msg, "content": text}
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def call_llm(
    *,
    agent: str,
    phase: str,
    run_id: str,
    messages: list[dict],
    temperature: float = 0,
    max_tokens: int = 1024,
    iteration: int = 0,
    snippet_text: str = "",   # raw snippet for hashing/preview — never logged as-is
    config_path: Path = CONFIG_PATH,
) -> str:
    """Call LiteLLM, log to llm_calls.jsonl, return response content string.

    Loads agent config (provider + model) from config.yaml, calls LiteLLM,
    appends a JSONL record to logs/YYYY-MM/llm_calls.jsonl on every call
    (success or failure), and appends an additional record to errors.jsonl
    on failure with full stack trace.

    Rate limit handling: retries up to 3 times with exponential backoff
    (10s, 20s, 40s) on RateLimitError. All other exceptions are logged and
    re-raised immediately without retry.

    Args:
        agent: Agent name matching an entry in config.yaml agents section.
        phase: Phase identifier, e.g. "phase2_bootstrap" or "phase3_analyse".
        run_id: Run identifier, e.g. "bootstrap" or a run slug.
        messages: List of {"role": ..., "content": ...} dicts for LiteLLM.
        temperature: Sampling temperature (default 0 for deterministic output).
        max_tokens: Maximum tokens in response (default 1024).
        iteration: Iteration counter within a multi-step agent loop (default 0).
        snippet_text: Raw snippet for hashing only. The raw text is never logged.
            Pass "" if no snippet context.
        config_path: Path to config.yaml (default CONFIG_PATH).

    Returns:
        Response content string from the LLM.

    Raises:
        FileNotFoundError: If config.yaml is missing.
        ValueError: If agent config is missing/incomplete.
        litellm.RateLimitError: If all retries are exhausted.
        Any other exception raised by litellm.completion is re-raised after logging.
    """
    agent_cfg = load_agent_config(agent, config_path)
    provider = agent_cfg["provider"]
    model = agent_cfg["model"]
    litellm_model = f"{provider}/{model}"

    snippet_hash = _sha256(snippet_text[:SNIPPET_LOG_MAX]) if snippet_text else ""
    snippet_chars = len(snippet_text) if snippet_text else 0

    # Retry configuration for rate-limit errors (429).
    # Exponential backoff: 10s, 20s, 40s — covers Tier-1 token-per-minute windows.
    _RETRY_DELAYS = [10, 20, 40]

    t0 = time.monotonic()
    prepared = _prepare_messages_for_provider(messages, provider)
    call_kwargs: dict[str, Any] = {"model": litellm_model, "messages": prepared, "max_tokens": max_tokens}
    # Claude 4.x models don't accept the temperature parameter.
    # is_claude4 is True for modern claude-* models that lack a 3.x/2.x/1.x marker.
    is_claude4 = model.startswith("claude-") and not any(
        x in model for x in ["-3-", "-haiku-3", "-sonnet-3", "claude-2", "claude-1"]
    )
    if not is_claude4:
        call_kwargs["temperature"] = temperature

    last_exc: Exception | None = None
    for attempt, _delay in enumerate([0] + _RETRY_DELAYS):
        if _delay:
            log.warning(
                "call_llm.rate_limit_retry",
                agent=agent, attempt=attempt, wait_s=_delay,
            )
            time.sleep(_delay)
        try:
            resp = litellm.completion(**call_kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            usage = getattr(resp, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            # Anthropic prompt-cache metrics — exposed via litellm as either
            # top-level attrs on usage or nested in usage["cache_read_input_tokens"].
            # When >0 we know the static prefix actually hit cache; when 0 across
            # multiple calls with the same prefix, caching is silently broken.
            cache_read = 0
            cache_creation = 0
            if usage is not None:
                cache_read = (
                    getattr(usage, "cache_read_input_tokens", None)
                    or getattr(usage, "prompt_cache_hit_tokens", None)
                    or 0
                )
                cache_creation = (
                    getattr(usage, "cache_creation_input_tokens", None)
                    or getattr(usage, "prompt_cache_miss_tokens", None)
                    or 0
                )
                if not cache_read and isinstance(usage, dict):
                    cache_read = usage.get("cache_read_input_tokens", 0)
                    cache_creation = usage.get("cache_creation_input_tokens", 0)
            cost = float(getattr(resp, "_hidden_params", {}).get("response_cost") or 0.0)
            # Truncation sichtbar machen: bei finish_reason/stop_reason == "length"
            # wurde die Antwort wegen max_tokens abgeschnitten. Stiller Verlust
            # (z.B. abgeschnittenes JSON beim keyword_marker -> leere Struktur)
            # wird so im Log erkennbar; die Fallback-Semantik bleibt unveraendert.
            finish_reason = (
                getattr(resp.choices[0], "finish_reason", None)
                or getattr(resp.choices[0], "stop_reason", None)
            )
            truncated = finish_reason in ("length", "max_tokens")
            _append_jsonl(_llm_log_path(), {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "agent": agent,
                "provider": provider,
                "model": model,
                "phase": phase,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": int(cache_read or 0),
                "cache_creation_input_tokens": int(cache_creation or 0),
                "cost_usd": cost,
                "duration_ms": duration_ms,
                "status": "success",
                "truncated": truncated,
                "error": None,
                "iteration": iteration,
                "snippet_hash": snippet_hash,
                "snippet_chars": snippet_chars,
            })
            if truncated:
                log.warning(
                    "call_llm.response_truncated",
                    agent=agent,
                    phase=phase,
                    run_id=run_id,
                    finish_reason=finish_reason,
                    max_tokens=max_tokens,
                )
            return resp.choices[0].message.content or ""
        except litellm.RateLimitError as exc:
            last_exc = exc
            duration_ms = int((time.monotonic() - t0) * 1000)
            err_str = f"{type(exc).__name__}: {exc}"
            _append_jsonl(_llm_log_path(), {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "agent": agent,
                "provider": provider,
                "model": model,
                "phase": phase,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "duration_ms": duration_ms,
                "status": "rate_limit",
                "error": err_str,
                "iteration": iteration,
                "snippet_hash": snippet_hash,
                "snippet_chars": snippet_chars,
            })
            # Retry if attempts remain; otherwise fall through to raise below.
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            err_str = f"{type(exc).__name__}: {exc}"
            _append_jsonl(_llm_log_path(), {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "agent": agent,
                "provider": provider,
                "model": model,
                "phase": phase,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "duration_ms": duration_ms,
                "status": "error",
                "error": err_str,
                "iteration": iteration,
                "snippet_hash": snippet_hash,
                "snippet_chars": snippet_chars,
            })
            _append_jsonl(_error_log_path(), {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "agent": agent,
                "phase": phase,
                "error": err_str,
                "stack_trace": traceback.format_exc(),
                "snippet_hash": snippet_hash,
                "snippet_chars": snippet_chars,
            })
            raise

    # All rate-limit retries exhausted — log to errors.jsonl and re-raise.
    assert last_exc is not None
    err_str = f"{type(last_exc).__name__}: {last_exc}"
    _append_jsonl(_error_log_path(), {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "agent": agent,
        "phase": phase,
        "error": err_str,
        "stack_trace": traceback.format_exc(),
        "snippet_hash": snippet_hash,
        "snippet_chars": snippet_chars,
    })
    raise last_exc
