"""Parameter pro Modellfamilie: temperature und reasoning_effort.

gpt-6-luna (Nachfolger von gpt-4.1, Abschaltung 14.01.2027) ist ein
Reasoning-Modell: LiteLLM lehnt temperature=0 ab, solange die Denktiefe
nicht `none` ist. Claude ab 4.x bekommt nie eine Temperatur.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cv_tailor import llm
from cv_tailor.cost_tracking import DAILY_LIMIT_ENV


def _call(monkeypatch, tmp_path, agent_line: str) -> dict:
    config = tmp_path / "config.yaml"
    config.write_text(f"agents:\n  writer: {agent_line}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(llm, "_cost_limit_cfg_cache", {})
    monkeypatch.setattr(llm, "_config_cache", {})
    seen: dict = {}
    resp = SimpleNamespace(
        usage=None,
        _hidden_params={"response_cost": 0.0},
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))],
    )

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return resp

    monkeypatch.setattr(llm.litellm, "completion", fake_completion)
    llm.call_llm(
        agent="writer",
        phase="test",
        run_id="X",
        messages=[{"role": "user", "content": "hi"}],
        config_path=config,
    )
    return seen


def test_gpt41_keeps_temperature(monkeypatch, tmp_path):
    seen = _call(monkeypatch, tmp_path, "{ provider: openai, model: gpt-4.1 }")
    assert seen["temperature"] == 0
    assert "reasoning_effort" not in seen


def test_reasoning_model_drops_temperature_by_default(monkeypatch, tmp_path):
    seen = _call(monkeypatch, tmp_path, "{ provider: openai, model: gpt-6-luna }")
    assert "temperature" not in seen


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_reasoning_effort_passed_and_temperature_dropped(monkeypatch, tmp_path, effort):
    seen = _call(
        monkeypatch,
        tmp_path,
        f"{{ provider: openai, model: gpt-6-luna, reasoning_effort: {effort} }}",
    )
    assert seen["reasoning_effort"] == effort
    assert "temperature" not in seen


def test_reasoning_effort_none_keeps_temperature(monkeypatch, tmp_path):
    seen = _call(
        monkeypatch, tmp_path, "{ provider: openai, model: gpt-6-luna, reasoning_effort: none }"
    )
    assert seen["reasoning_effort"] == "none"
    assert seen["temperature"] == 0


def test_claude_never_gets_temperature(monkeypatch, tmp_path):
    seen = _call(monkeypatch, tmp_path, "{ provider: anthropic, model: claude-sonnet-4-6 }")
    assert "temperature" not in seen


def test_thinking_reserve_is_added_to_max_tokens(monkeypatch, tmp_path):
    seen = _call(
        monkeypatch,
        tmp_path,
        "{ provider: anthropic, model: claude-sonnet-5-5, reasoning_effort: low, "
        "thinking_reserve_tokens: 8000 }",
    )
    assert seen["max_tokens"] == 1024 + 8000
    assert seen["reasoning_effort"] == "low"
    assert "temperature" not in seen


def test_no_reserve_keeps_max_tokens(monkeypatch, tmp_path):
    seen = _call(monkeypatch, tmp_path, "{ provider: anthropic, model: claude-haiku-4-5 }")
    assert seen["max_tokens"] == 1024
