"""Tageslimit fuer LLM-Kosten: Summe pro UTC-Tag, Abbruch vor dem Call."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_tailor import llm
from cv_tailor.cost_tracking import (
    DAILY_LIMIT_ENV,
    DailyCostLimitExceeded,
    check_daily_limit,
    compute_daily_cost,
    resolve_daily_limit,
)


def _write_log(root: Path, records: list) -> None:
    now = datetime.now(UTC)
    month_dir = root / now.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    with (month_dir / "llm_calls.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")


@pytest.fixture
def today_logs(tmp_path: Path) -> Path:
    now = datetime.now(UTC)
    today = now.isoformat()
    # Ein anderer Tag im selben Monat, sofern es ihn gibt (am 1. faellt er weg).
    other_day = (now - timedelta(days=1)).isoformat() if now.day > 1 else "1999-01-01T00:00:00"
    _write_log(
        tmp_path,
        [
            {"timestamp": today, "run_id": "A", "status": "success", "cost_usd": 1.25},
            {"timestamp": today, "run_id": "B", "status": "success", "cost_usd": 0.75},
            {"timestamp": today, "run_id": "B", "status": "error", "cost_usd": 9.0},
            {"timestamp": today, "run_id": "B", "status": "rate_limit"},
            {"timestamp": other_day, "run_id": "C", "status": "success", "cost_usd": 50.0},
            {"run_id": "D", "status": "success", "cost_usd": 3.0},  # ohne Zeitstempel
            "kein json",
        ],
    )
    return tmp_path


def test_compute_daily_cost_sums_only_todays_successes(today_logs):
    assert compute_daily_cost(log_root=today_logs) == pytest.approx(2.0)


def test_compute_daily_cost_without_log_is_zero(tmp_path):
    assert compute_daily_cost(log_root=tmp_path) == 0.0


def test_check_daily_limit_raises_when_reached(today_logs):
    with pytest.raises(DailyCostLimitExceeded) as info:
        check_daily_limit(2.0, log_root=today_logs)
    assert info.value.spent_usd == pytest.approx(2.0)
    assert info.value.limit_usd == 2.0


def test_check_daily_limit_passes_below_limit(today_logs):
    assert check_daily_limit(5.0, log_root=today_logs) == pytest.approx(2.0)


def test_check_daily_limit_off_reads_nothing(tmp_path):
    assert check_daily_limit(None, log_root=tmp_path / "fehlt") == 0.0


@pytest.mark.parametrize(
    ("config", "env", "expected"),
    [
        ({"cost_limits": {"daily_usd": 25}}, None, 25.0),
        ({"cost_limits": {"daily_usd": 0}}, None, None),
        ({}, None, None),
        ({"cost_limits": "kaputt"}, None, None),
        ({"cost_limits": {"daily_usd": 25}}, "3.5", 3.5),
        ({"cost_limits": {"daily_usd": 25}}, "0", None),
        ({"cost_limits": {"daily_usd": 25}}, "abc", None),
        ({"cost_limits": {"daily_usd": 25}}, "", 25.0),
    ],
)
def test_resolve_daily_limit(monkeypatch, config, env, expected):
    if env is None:
        monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    else:
        monkeypatch.setenv(DAILY_LIMIT_ENV, env)
    assert resolve_daily_limit(config) == expected


def test_repo_config_sets_a_daily_limit(monkeypatch):
    """config.yaml traegt ein aktives Limit; es darf nicht still verschwinden."""
    import yaml

    monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert resolve_daily_limit(cfg) and resolve_daily_limit(cfg) > 0


def test_call_llm_aborts_before_provider_call(monkeypatch, tmp_path):
    """Bei erreichtem Limit geht kein Call an den Provider raus.

    chdir nach tmp_path: LOG_ROOT ist relativ (`logs/`), liest also die hier
    geschriebene Datei.
    """
    config = tmp_path / "config.yaml"
    config.write_text(
        "cost_limits:\n  daily_usd: 1\nagents:\n  writer: { provider: openai, model: gpt-4.1 }\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(llm, "_cost_limit_cfg_cache", {})
    monkeypatch.setattr(llm, "_config_cache", {})
    _write_log(
        tmp_path / "logs",
        [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "run_id": "X",
                "status": "success",
                "cost_usd": 1.5,
            },
        ],
    )
    called = []
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: called.append(kw))

    with pytest.raises(DailyCostLimitExceeded):
        llm.call_llm(
            agent="writer",
            phase="test",
            run_id="X",
            messages=[{"role": "user", "content": "hi"}],
            config_path=config,
        )
    assert called == []
    errors = list((tmp_path / "logs").glob("*/errors.jsonl"))
    assert errors and "DailyCostLimitExceeded" in errors[0].read_text(encoding="utf-8")


def test_call_llm_runs_when_limit_disabled(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("agents:\n  writer: { provider: openai, model: gpt-4.1 }\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(llm, "_cost_limit_cfg_cache", {})
    monkeypatch.setattr(llm, "_config_cache", {})
    resp = SimpleNamespace(
        usage=None,
        _hidden_params={"response_cost": 0.01},
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))],
    )
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: resp)
    out = llm.call_llm(
        agent="writer",
        phase="test",
        run_id="X",
        messages=[{"role": "user", "content": "hi"}],
        config_path=config,
    )
    assert out == "ok"
