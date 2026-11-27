"""Shared fixtures for the cv-tailor test suite.

These tests intentionally do NOT exercise LLM-driven code paths — that
domain is covered by `evals/run.py` against real run artifacts. The
pytest suite focuses on the deterministic helpers (regex parsers, word
counters, topic classifiers, validators, aggregators) where every input
maps to exactly one expected output.
"""
from __future__ import annotations

import socket as _socket
import sys
from pathlib import Path

import pytest as _pytest

# Make `import cv_tailor.*` work from a clean checkout without an editable
# install. Mirrors what `uv run pytest` does implicitly via the project
# install, but keeps `python -m pytest` viable too.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# --- Netzsperre ------------------------------------------------------------
# Kein Test darf ins Netz: ein versehentlicher echter LLM-Aufruf wuerde
# Lebenslauf-Inhalte an eine API schicken und Geld kosten. Uebernommen aus
# shoppingassistent (Netzsperre in den Unit-Tests) und buch-app. Erlaubt ist
# nur die eigene Maschine (127.0.0.1, ::1, Unix-Sockets), z. B. fuer lokale
# Testserver. Wer bewusst ins Netz will, gehoert in evals/, nicht in pytest.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ORIG_CONNECT = _socket.socket.connect
_ORIG_CONNECT_EX = _socket.socket.connect_ex


def _ist_lokal(address) -> bool:
    if isinstance(address, (str, bytes)):  # Unix-Socket-Pfad
        return True
    host = address[0] if isinstance(address, tuple) and address else address
    return isinstance(host, str) and host in _LOOPBACK


class NetzZugriffImTest(RuntimeError):
    """Ein Test hat versucht, eine Verbindung ausserhalb der Maschine aufzubauen."""


def _gesperrt(orig):
    def connect(self, address, *args, **kwargs):
        if not _ist_lokal(address):
            raise NetzZugriffImTest(
                f"Netzzugriff im Test gesperrt: {address!r}. "
                "Echte Aufrufe gehoeren in evals/, nicht in pytest."
            )
        return orig(self, address, *args, **kwargs)

    return connect


@_pytest.fixture(autouse=True)
def _netzsperre(monkeypatch):
    monkeypatch.setattr(_socket.socket, "connect", _gesperrt(_ORIG_CONNECT))
    monkeypatch.setattr(_socket.socket, "connect_ex", _gesperrt(_ORIG_CONNECT_EX))
    yield


# --- Ausgangssperre ----------------------------------------------------------
# Die lokale Verbotsliste (data/ausgangssperre.local.yaml) gehoert zu Alex'
# Maschine, nicht zur Testsuite: Tests laufen ohne sie, damit sie ueberall
# gleich ausfallen und nie echte Werte lesen. Die Variable zeigt auf eine
# leere Liste: eine fehlende Datei hinter der Variable bricht ab (fail closed).
@_pytest.fixture(autouse=True)
def _ohne_lokale_verbotsliste(monkeypatch, tmp_path_factory):
    # Eigener Ordner, damit tmp_path der Tests leer bleibt.
    leer = tmp_path_factory.mktemp("ausgangssperre") / "leere_verbotsliste.yaml"
    leer.write_text("verboten: []\n", encoding="utf-8")
    monkeypatch.setenv("CV_TAILOR_AUSGANGSSPERRE_LISTE", str(leer))
    yield
