"""Idee 47 (Katalog C2): Der Beleg-Index schluckt keine Abbruchfehler mehr.

Bis 2026-11-23 fing ``_classify_one`` jede Ausnahme und legte den Beleg still
als «other» ab. Eine gesperrte Ausgabe, ein erreichtes Tageslimit oder ein
falscher API-Schluessel machten den Index so unbemerkt schlechter. Diese Fehler
muessen jetzt durchschlagen; der Fallback bleibt fuer nicht lesbare Antworten.
Alles synthetisch, kein Netz.
"""
from __future__ import annotations

import httpx
import litellm
import pytest

from cv_tailor import beleg_index
from cv_tailor.ausgangssperre import AusgangGesperrt, Fund
from cv_tailor.cost_tracking import DailyCostLimitExceeded

_CLAIM = beleg_index.RawClaim(
    snippet="Leitete ein Team von fuenf Personen.",
    quelle_datei="standard_cv.md",
    quelle_position="line:1",
    quelle_typ="standard_cv",
    kontext="",
    section="Berufserfahrung",
)


def _werfe(exc):
    def fake_call_llm(**_kw):
        raise exc

    return fake_call_llm


def _auth_fehler():
    return litellm.AuthenticationError(
        message="ungueltiger Schluessel", llm_provider="anthropic", model="m"
    )


def _rechte_fehler():
    antwort = httpx.Response(403, request=httpx.Request("POST", "https://example.invalid"))
    return litellm.PermissionDeniedError(
        message="kein Zugriff", llm_provider="anthropic", model="m", response=antwort
    )


@pytest.mark.parametrize(
    "fabrik",
    [
        lambda: AusgangGesperrt([Fund(art="ahv", nachricht=0, rolle="user")], agent="factcheck"),
        lambda: DailyCostLimitExceeded(30.0, 25.0),
        _auth_fehler,
        _rechte_fehler,
    ],
    ids=["ausgangssperre", "tageslimit", "auth", "rechte"],
)
def test_abbruchfehler_werden_weitergeworfen(monkeypatch, fabrik):
    exc = fabrik()
    monkeypatch.setattr(beleg_index, "call_llm", _werfe(exc))
    with pytest.raises(type(exc)):
        beleg_index._classify_one(_CLAIM)


def test_unlesbare_antwort_faellt_auf_other_zurueck(monkeypatch):
    monkeypatch.setattr(beleg_index, "call_llm", lambda **_kw: "kein json")
    assert beleg_index._classify_one(_CLAIM) == ("other", _CLAIM.snippet)


def test_voruebergehender_anbieterfehler_faellt_auf_other_zurueck(monkeypatch):
    """Ein einzelner Netz- oder Serverfehler bricht den Bootstrap nicht ab."""
    exc = litellm.APIConnectionError(message="weg", llm_provider="anthropic", model="m")
    monkeypatch.setattr(beleg_index, "call_llm", _werfe(exc))
    assert beleg_index._classify_one(_CLAIM) == ("other", _CLAIM.snippet)
