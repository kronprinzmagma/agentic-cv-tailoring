"""Sicherheitstests für die lokale Web-UI (Code-Review 2026-11-23).

Deckt die CodeQL-Funde im Public-Klon ab (py/path-injection, py/http-response-splitting)
und den Schutz gegen DNS-Rebinding und Cross-Site-Requests. Die Tests rufen den Handler
ohne Socket auf, damit sie auch in einer Sandbox ohne lokale Ports laufen.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_tailor import web


def _handler(
    path: str, headers: dict[str, str], *, method: str = "GET", body: bytes = b""
) -> web.CvTailorHandler:
    h = web.CvTailorHandler.__new__(web.CvTailorHandler)
    h.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
    h.client_address = ("127.0.0.1", 50000)
    h.request_version = "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.command = method
    h.path = path
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    if body:
        msg["Content-Length"] = str(len(body))
    h.headers = msg
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    return h


def _response(h: web.CvTailorHandler) -> tuple[int, dict[str, str], bytes]:
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, body


# --- Host und Origin -------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "origin", "erlaubt"),
    [
        ("127.0.0.1:8765", None, True),
        ("localhost:8765", None, True),
        ("[::1]:8765", None, True),
        ("127.0.0.1:8765", "http://127.0.0.1:8765", True),
        ("localhost:8765", "http://localhost:8765", True),
        # DNS-Rebinding: fremder Name zeigt auf 127.0.0.1
        ("evil.example:8765", None, False),
        ("evil.example:8765", "http://evil.example:8765", False),
        # Cross-Site-POST aus einem fremden Tab
        ("127.0.0.1:8765", "https://evil.example", False),
        ("127.0.0.1:8765", "null", False),
        # Origin mit anderem Loopback-Namen ist eine andere Herkunft
        ("127.0.0.1:8765", "http://localhost:8765", False),
        # falscher oder fehlender Port, fehlender Host
        ("127.0.0.1:9999", None, False),
        ("127.0.0.1", None, False),
        (None, None, False),
    ],
)
def test_request_origin_allowed(host, origin, erlaubt):
    assert web._request_origin_allowed(host, origin, "127.0.0.1", 8765) is erlaubt


def test_wildcard_bind_erlaubt_keine_fremden_hosts():
    assert not web._request_origin_allowed("192.168.1.5:8765", None, "0.0.0.0", 8765)
    assert web._request_origin_allowed("127.0.0.1:8765", None, "0.0.0.0", 8765)


def test_get_mit_fremdem_host_wird_abgelehnt():
    h = _handler("/api/health", {"Host": "evil.example:8765"})
    h.do_GET()
    status, _, body = _response(h)
    assert status == 403
    assert "fremder Herkunft" in json.loads(body)["error"]


def test_get_mit_lokalem_host_funktioniert():
    h = _handler("/api/health", {"Host": "127.0.0.1:8765"})
    h.do_GET()
    status, _, body = _response(h)
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_cross_site_post_startet_keinen_lauf(monkeypatch):
    aufgerufen = []
    monkeypatch.setattr(
        web.CvTailorHandler, "_handle_create_run", lambda self: aufgerufen.append(True)
    )
    body = json.dumps({"filename": "x.md", "content": "Anzeige"}).encode()
    h = _handler(
        "/api/runs",
        {"Host": "127.0.0.1:8765", "Origin": "https://evil.example", "Content-Type": "text/plain"},
        method="POST",
        body=body,
    )
    h.do_POST()
    status, _, _ = _response(h)
    assert status == 403
    assert aufgerufen == []


def test_post_aus_eigener_oberflaeche_wird_angenommen(monkeypatch):
    aufgerufen = []
    monkeypatch.setattr(
        web.CvTailorHandler, "_handle_create_run", lambda self: aufgerufen.append(True)
    )
    h = _handler(
        "/api/runs",
        {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"},
        method="POST",
    )
    h.do_POST()
    assert aufgerufen == [True]


# --- CodeQL py/http-response-splitting (web.py _send_text/_send_binary) -------------


@pytest.mark.parametrize("sender", ["text", "binary"])
def test_content_disposition_kodiert_zeilenumbrueche(sender):
    boeser_name = "cv.md\r\nSet-Cookie: x=1\r\n\r\n<script>"
    h = _handler("/", {"Host": "127.0.0.1:8765"})
    if sender == "text":
        h._send_text("Inhalt", file_name=boeser_name, download=True)
    else:
        h._send_binary(b"%PDF", file_name=boeser_name, content_type="application/pdf")
    status, headers, _ = _response(h)
    assert status == 200
    assert "set-cookie" not in headers
    assert "\r" not in headers["content-disposition"]
    assert "%0D%0A" in headers["content-disposition"]


# --- CodeQL py/path-injection (web.py Upload und Dateiabruf) --------------------------


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "/abs/pfad.md", "..", "a/../../b.md", "x\x00.md", ""]
)
def test_safe_filename_bleibt_im_upload_ordner(tmp_path, name):
    sicher = web._safe_filename(name)
    assert "/" not in sicher and "\\" not in sicher and "\x00" not in sicher
    ziel = (tmp_path / sicher).resolve()
    assert ziel.parent == tmp_path.resolve()


def _job_mit_run(tmp_path, monkeypatch) -> Path:
    runs = tmp_path / "runs"
    run_dir = runs / "lauf1"
    run_dir.mkdir(parents=True)
    (run_dir / "01_analyse.md").write_text("# Analyse\n", encoding="utf-8")
    (tmp_path / "geheim.md").write_text("geheim\n", encoding="utf-8")
    monkeypatch.setattr(web, "RUNS_ROOT", runs.resolve())
    state = web.WebState.__new__(web.WebState)
    state._lock = __import__("threading").Lock()
    state._jobs = {"job1": web.WebJob("job1", status="completed", run_dir=str(run_dir))}
    monkeypatch.setattr(web, "STATE", state)
    return run_dir


@pytest.mark.parametrize(
    "datei", ["..%2Fgeheim.md", "%2E%2E%2Fgeheim.md", "..%5Cgeheim.md", "geheim.md"]
)
def test_dateiabruf_nur_aus_erlaubnisliste(tmp_path, monkeypatch, datei):
    _job_mit_run(tmp_path, monkeypatch)
    h = _handler(f"/api/runs/job1/files/{datei}", {"Host": "127.0.0.1:8765"})
    h.do_GET()
    status, _, body = _response(h)
    assert status == 403
    assert b"geheim\n" not in body


def test_dateiabruf_erlaubter_datei_funktioniert(tmp_path, monkeypatch):
    _job_mit_run(tmp_path, monkeypatch)
    h = _handler("/api/runs/job1/files/01_analyse.md", {"Host": "127.0.0.1:8765"})
    h.do_GET()
    status, headers, body = _response(h)
    assert status == 200
    assert body == b"# Analyse\n"
    assert headers["content-type"].startswith("text/markdown")


def test_dateiabruf_ueber_symlink_aus_runs_hinaus_wird_abgelehnt(tmp_path, monkeypatch):
    run_dir = _job_mit_run(tmp_path, monkeypatch)
    (run_dir / "01_analyse.md").unlink()
    (run_dir / "01_analyse.md").symlink_to(tmp_path / "geheim.md")
    h = _handler("/api/runs/job1/files/01_analyse.md", {"Host": "127.0.0.1:8765"})
    h.do_GET()
    status, _, body = _response(h)
    assert status == 403
    assert b"geheim\n" not in body
