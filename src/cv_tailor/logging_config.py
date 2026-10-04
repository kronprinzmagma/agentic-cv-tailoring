"""structlog setup for cv-tailor.

Centralized logging configuration used by every agent in later phases.

Two output sinks:
  - Terminal (stderr): ConsoleRenderer — colored, human-readable.
  - File (optional): JSONRenderer — one event per line, suitable for
    `logs/YYYY-MM/llm_calls.jsonl` in Phase 3.

Sensitive data (CV / Zeugnis snippets) is NOT redacted here. Redaction is the
caller's responsibility — see CLAUDE.md "Sensible Daten" (max 200 chars + hash).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(
    *,
    json_path: Path | None = None,
    level: int = logging.INFO,
) -> None:
    """Configure structlog + stdlib logging.

    Args:
      json_path: If given, JSON-serialised events are appended to this file.
                 Parent directory must already exist (caller's responsibility).
      level:     stdlib logging level (default INFO).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Console handler (stderr, human-readable).
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)

    handlers: list[logging.Handler] = [console_handler]

    # Optional JSON file handler.
    if json_path is not None:
        json_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        file_handler = logging.FileHandler(json_path, encoding="utf-8")
        file_handler.setFormatter(json_formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    # Replace any pre-existing handlers to keep configuration deterministic.
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Calls `configure_logging()` if not yet configured."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
