"""`cv-tailor render-pdf`: eigener Befehl für das PDF (Sandbox-Ausnahme)."""
from click.testing import CliRunner

from cv_tailor import cli as cli_mod
from cv_tailor import pdf_renderer


def _run(tmp_path, monkeypatch, args, written=None, raises=None):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs" / "lauf").mkdir(parents=True)
    seen = {}

    def fake(ctx, template_path):
        seen["template"] = template_path
        if raises:
            raise raises
        return written or []

    monkeypatch.setattr(pdf_renderer, "run_pdf_renderer", fake)
    for name in ("TEMPLATE_PATH", "TEMPLATE_OHNE_FOTO_PATH"):
        t = tmp_path / f"{name}.html"
        t.write_text("x")
        monkeypatch.setattr(pdf_renderer, name, t)
    return CliRunner().invoke(cli_mod.cli, ["render-pdf", *args]), seen


def test_waehlt_template_ohne_foto(tmp_path, monkeypatch):
    res, seen = _run(tmp_path, monkeypatch, ["lauf", "--ohne-foto"], written=[tmp_path / "a.pdf"])
    assert res.exit_code == 0 and seen["template"].name == "TEMPLATE_OHNE_FOTO_PATH.html"


def test_standard_template(tmp_path, monkeypatch):
    res, seen = _run(tmp_path, monkeypatch, ["lauf"], written=[tmp_path / "a.pdf"])
    assert res.exit_code == 0 and seen["template"].name == "TEMPLATE_PATH.html"


def test_offene_punkte_sperren(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, ["lauf"], raises=pdf_renderer.FaktencheckOffen("offen"))
    assert res.exit_code == 1 and "Kein PDF" in res.output


def test_kein_pdf_ist_fehler(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, ["lauf"])
    assert res.exit_code == 1


def test_pfad_ausserhalb_runs_abgelehnt(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, ["../etwas"])
    assert res.exit_code == 2
