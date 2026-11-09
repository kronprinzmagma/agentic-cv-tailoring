"""Tests for the prompt-cache + dev-mode reload."""
import time
from pathlib import Path

import pytest

import cv_tailor.llm as llm
from cv_tailor.llm import load_prompt


@pytest.fixture
def clear_cache():
    """Reset the in-process prompt cache between tests."""
    llm._prompt_cache.clear()
    yield
    llm._prompt_cache.clear()


def test_load_prompt_reads_file(tmp_path: Path, clear_cache):
    p = tmp_path / "writer.md"
    p.write_text("Hello prompt", encoding="utf-8")
    assert load_prompt(p) == "Hello prompt"


def test_load_prompt_caches_by_default(tmp_path: Path, clear_cache, monkeypatch):
    """Without DEV_RELOAD: an edit after the first load is NOT visible until
    the process restarts. Matches the documented production behaviour."""
    monkeypatch.delenv("CV_TAILOR_DEV_RELOAD", raising=False)
    p = tmp_path / "writer.md"
    p.write_text("v1", encoding="utf-8")
    assert load_prompt(p) == "v1"
    time.sleep(0.01)  # ensure mtime would advance
    p.write_text("v2", encoding="utf-8")
    assert load_prompt(p) == "v1", "production cache must be sticky"


def test_load_prompt_dev_reload_picks_up_edits(tmp_path: Path, clear_cache, monkeypatch):
    """CV_TAILOR_DEV_RELOAD=1: an edit becomes visible on the next call."""
    monkeypatch.setenv("CV_TAILOR_DEV_RELOAD", "1")
    p = tmp_path / "writer.md"
    p.write_text("v1", encoding="utf-8")
    assert load_prompt(p) == "v1"
    time.sleep(0.01)  # cross the mtime resolution boundary
    p.write_text("v2-edited", encoding="utf-8")
    assert load_prompt(p) == "v2-edited"


def test_load_prompt_dev_reload_skips_when_unchanged(tmp_path: Path, clear_cache, monkeypatch):
    """Dev-mode still avoids re-reading when mtime hasn't advanced."""
    monkeypatch.setenv("CV_TAILOR_DEV_RELOAD", "1")
    p = tmp_path / "writer.md"
    p.write_text("v1", encoding="utf-8")
    a = load_prompt(p)
    b = load_prompt(p)
    assert a == b == "v1"


def test_load_prompt_missing_raises(tmp_path: Path, clear_cache):
    with pytest.raises(FileNotFoundError):
        load_prompt(tmp_path / "missing.md")
