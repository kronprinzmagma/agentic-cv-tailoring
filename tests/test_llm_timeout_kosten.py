"""Idee 48 (Katalog C3): Timeout, Tageslimit bei unbekanntem Preis, Logging.

- Jeder Aufruf hat ein Timeout aus config.yaml (``llm.timeout_s``) statt des
  LiteLLM-Standards von 6000 s; ein Timeout wird genau einmal wiederholt.
- Liefert LiteLLM keinen Preis, wird der Aufruf nicht mehr mit 0 USD verbucht,
  sondern konservativ aus den Tokens geschaetzt: sonst greift das Tageslimit nie.
- errors.jsonl enthaelt auch nach erschoepften Wiederholungen den echten
  Traceback (vorher «NoneType: None»).
Alles synthetisch, kein Netz.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from cv_tailor import llm
from cv_tailor.cost_tracking import DAILY_LIMIT_ENV, FALLBACK_USD_PER_MTOK, estimate_cost_usd


def _resp(cost, in_tok=1000, out_tok=500):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok),
        _hidden_params={"response_cost": cost},
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))],
    )


@pytest.fixture
def umgebung(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DAILY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(llm, "_cost_limit_cfg_cache", {})
    monkeypatch.setattr(llm, "_config_cache", {})
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)

    def schreibe(extra: str = "", agent: str = "{ provider: anthropic, model: claude-x }"):
        config = tmp_path / "config.yaml"
        config.write_text(f"{extra}agents:\n  writer: {agent}\n", encoding="utf-8")
        return config

    return schreibe


def _call(config):
    return llm.call_llm(
        agent="writer",
        phase="test",
        run_id="X",
        messages=[{"role": "user", "content": "hi"}],
        config_path=config,
    )


def _jsonl(name: str) -> list[dict]:
    dateien = list(Path("logs").glob(f"*/{name}"))
    assert dateien, name
    return [json.loads(z) for z in dateien[0].read_text(encoding="utf-8").splitlines()]


def _timeout():
    return litellm.Timeout(message="zu langsam", model="claude-x", llm_provider="anthropic")


# --- Timeout -----------------------------------------------------------------

def test_timeout_aus_config(monkeypatch, umgebung):
    config = umgebung("llm:\n  timeout_s: 200\n")
    seen = {}
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: seen.update(kw) or _resp(0.01))
    _call(config)
    assert seen["timeout"] == 200


def test_timeout_standard_ohne_eintrag(monkeypatch, umgebung):
    config = umgebung()
    seen = {}
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: seen.update(kw) or _resp(0.01))
    _call(config)
    assert seen["timeout"] == llm.DEFAULT_TIMEOUT_S
    assert 180 <= llm.DEFAULT_TIMEOUT_S <= 300


@pytest.mark.parametrize("wert", ["0", "-5", "abc", "100000"])
def test_ungueltiges_timeout_faellt_auf_standard(monkeypatch, umgebung, wert):
    config = umgebung(f"llm:\n  timeout_s: {wert}\n")
    seen = {}
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: seen.update(kw) or _resp(0.01))
    _call(config)
    assert seen["timeout"] == llm.DEFAULT_TIMEOUT_S


def test_timeout_wird_einmal_wiederholt(monkeypatch, umgebung):
    config = umgebung()
    antworten = [_timeout(), _resp(0.01)]
    aufrufe = []

    def fake(**kw):
        aufrufe.append(kw)
        a = antworten.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(llm.litellm, "completion", fake)
    assert _call(config) == "ok"
    assert len(aufrufe) == 2
    assert [r["status"] for r in _jsonl("llm_calls.jsonl")] == ["timeout", "success"]


def test_zweites_timeout_bricht_ab_mit_traceback(monkeypatch, umgebung):
    config = umgebung()
    aufrufe = []

    def fake(**kw):
        aufrufe.append(kw)
        raise _timeout()

    monkeypatch.setattr(llm.litellm, "completion", fake)
    with pytest.raises(litellm.Timeout):
        _call(config)
    assert len(aufrufe) == 2
    fehler = _jsonl("errors.jsonl")
    assert len(fehler) == 1
    assert "Timeout" in fehler[0]["stack_trace"]
    assert "NoneType: None" not in fehler[0]["stack_trace"]


def test_erschoepfte_rate_limits_loggen_echten_traceback(monkeypatch, umgebung):
    config = umgebung()

    def fake(**_kw):
        raise litellm.RateLimitError(message="429", llm_provider="anthropic", model="claude-x")

    monkeypatch.setattr(llm.litellm, "completion", fake)
    with pytest.raises(litellm.RateLimitError):
        _call(config)
    fehler = _jsonl("errors.jsonl")
    assert "RateLimitError" in fehler[-1]["stack_trace"]
    assert "NoneType: None" not in fehler[-1]["stack_trace"]


# --- Kosten bei unbekanntem Preis ---------------------------------------------

def test_estimate_cost_usd():
    kosten = estimate_cost_usd(1_000_000, 1_000_000)
    assert kosten == pytest.approx(FALLBACK_USD_PER_MTOK["input"] + FALLBACK_USD_PER_MTOK["output"])
    assert estimate_cost_usd(0, 0) == 0.0


@pytest.mark.parametrize("preis", [None, 0.0])
def test_unbekannter_preis_wird_geschaetzt(monkeypatch, umgebung, preis):
    config = umgebung()
    monkeypatch.setattr(llm.litellm, "completion", lambda **_kw: _resp(preis, 1000, 500))
    _call(config)
    rec = _jsonl("llm_calls.jsonl")[-1]
    assert rec["cost_usd"] == pytest.approx(estimate_cost_usd(1000, 500))
    assert rec["cost_usd"] > 0
    assert rec["cost_estimated"] is True


def test_bekannter_preis_bleibt(monkeypatch, umgebung):
    config = umgebung()
    monkeypatch.setattr(llm.litellm, "completion", lambda **_kw: _resp(0.0123))
    _call(config)
    rec = _jsonl("llm_calls.jsonl")[-1]
    assert rec["cost_usd"] == pytest.approx(0.0123)
    assert rec["cost_estimated"] is False


def test_lokales_modell_kostet_nichts(monkeypatch, umgebung):
    config = umgebung(agent="{ provider: ollama, model: gemma }")
    monkeypatch.setattr(llm.litellm, "completion", lambda **_kw: _resp(0.0))
    _call(config)
    rec = _jsonl("llm_calls.jsonl")[-1]
    assert rec["cost_usd"] == 0.0
    assert rec["cost_estimated"] is False


def test_geschaetzte_kosten_zaehlen_fuers_tageslimit(monkeypatch, umgebung):
    """Ohne Preis lief das Limit nie voll; jetzt bricht der naechste Aufruf ab."""
    from cv_tailor.cost_tracking import DailyCostLimitExceeded

    config = umgebung("cost_limits:\n  daily_usd: 0.01\n")
    monkeypatch.setattr(llm.litellm, "completion", lambda **_kw: _resp(None, 100_000, 100_000))
    _call(config)
    with pytest.raises(DailyCostLimitExceeded):
        _call(config)


def test_config_yaml_setzt_timeout_im_bereich():
    """Die echte config.yaml traegt ein Timeout zwischen 180 und 300 s."""
    import yaml

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
    assert 180 <= cfg["llm"]["timeout_s"] <= 300
