"""Shared fixtures for the cv-tailor test suite.

These tests intentionally do NOT exercise LLM-driven code paths — that
domain is covered by `evals/run.py` against real run artifacts. The
pytest suite focuses on the deterministic helpers (regex parsers, word
counters, topic classifiers, validators, aggregators) where every input
maps to exactly one expected output.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `import cv_tailor.*` work from a clean checkout without an editable
# install. Mirrors what `uv run pytest` does implicitly via the project
# install, but keeps `python -m pytest` viable too.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
