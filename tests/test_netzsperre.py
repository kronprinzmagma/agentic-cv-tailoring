"""Die Netzsperre aus conftest.py greift: Verbindungen nach aussen scheitern."""
from __future__ import annotations

import socket

import pytest

from tests.conftest import NetzZugriffImTest


def test_verbindung_nach_aussen_ist_gesperrt():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetzZugriffImTest):
            s.connect(("203.0.113.1", 443))  # TEST-NET-3, nie erreichbar
    finally:
        s.close()


def test_lokale_verbindung_bleibt_erlaubt():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()
