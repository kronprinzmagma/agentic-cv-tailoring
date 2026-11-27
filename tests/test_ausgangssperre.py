"""Ausgangssperre: geschuetzte Daten verlassen die Maschine nie.

Alle Werte sind synthetisch (Beispiel-AHV, Beispiel-IBAN aus der Doku der
Banken, Testkarte 4111…, erfundene Adressen). Secrets werden zur Laufzeit
zusammengesetzt, damit der gitleaks-Hook keinen Fehlalarm gibt.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_tailor import ausgangssperre, llm
from cv_tailor.ausgangssperre import (
    GEBURTSDATUM_ERSATZ,
    AusgangGesperrt,
    finde_im_text,
    lade_verbotsliste,
    ohne_geburtsdatum,
    ohne_kontaktblock,
    pruefe_nachrichten,
    sperre_pruefen,
)
from cv_tailor.beleg_index import format_beleg_index_compact
from cv_tailor.experience_activation import activate_entries

ROOT = Path(__file__).resolve().parent.parent
ROOT_VERBOTSLISTE = ROOT / "data" / "ausgangssperre.local.yaml"

AHV = "756.1234.5678.97"
# Beispiel-IBAN aus Bankdokumentationen; zusammengesetzt, damit gitleaks nicht anschlaegt.
IBAN = "CH93 0076 " + "2011 6238 5295 7"
IBAN_KOMPAKT = IBAN.replace(" ", "")
IBAN_FALSCHE_PRUEFZIFFER = IBAN[:-1] + "8"
KARTE = "4111 1111 1111 1111"
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "Q" * 40


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "art"),
    [
        (f"Versicherungsnummer {AHV}", "AHV-Nummer"),
        ("AHV 756 1234 5678 97", "AHV-Nummer"),
        ("AHV 7561234567897", "AHV-Nummer"),
        (f"Konto {IBAN}", "IBAN"),
        (f"Konto {IBAN_KOMPAKT}", "IBAN"),
        (f"Karte {KARTE}", "Kreditkartennummer"),
        ("**Geburtsdatum:** 01. Januar 1985", "Geburtsdatum"),
        ("Herr Muster, geboren am 3.4.1980, war bei uns tätig.", "Geburtsdatum"),
        ("**Date of Birth:** 01. January 1985", "Geburtsdatum"),
        ("born on March 3, 1980", "Geburtsdatum"),
        (f"key={ANTHROPIC_KEY}", "Secret/API-Key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "Secret/API-Key"),
    ],
)
def test_erkennt_geschuetzte_daten(text, art):
    assert art in finde_im_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "Alex Müller, Senior Product Manager, 2019–2024 bei DemoSaaS",
        "Team von 12 Personen, Budget CHF 1'200'000, 20% unter Plan",
        f"Ungueltige IBAN {IBAN_FALSCHE_PRUEFZIFFER}",
        "Bestellnummer 4111 1111 1111 1112",
        "Kontakt: +41 44 555 01 23",
        "Seit 01.03.2019 als Product Owner",
        "Die Stelle ist ab 1. Januar 2027 zu besetzen.",
    ],
)
def test_keine_fehlalarme_auf_normalem_cv_text(text):
    assert finde_im_text(text) == []


def test_eigener_api_key_aus_umgebung(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "eigener-schluessel-ohne-bekanntes-format-123")
    assert finde_im_text("x eigener-schluessel-ohne-bekanntes-format-123 y") == ["Secret/API-Key"]


# ---------------------------------------------------------------------------
# Verbotsliste
# ---------------------------------------------------------------------------

def _liste(tmp_path, text: str) -> Path:
    p = tmp_path / "ausgangssperre.local.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_verbotsliste_tolerant_gegen_trenner_und_schreibweise(tmp_path):
    liste = lade_verbotsliste(_liste(tmp_path, (
        "verboten:\n"
        "  - { wert: '+41 79 000 11 22', art: Telefon privat }\n"
        "  - { wert: 'Musterweg 7', art: Privatadresse }\n"
        "  - Geheimprojekt Kolibri\n"
    )))
    assert finde_im_text("Mobile: +41-79-000-11-22", liste) == ["Verbotsliste: Telefon privat"]
    assert finde_im_text("wohnhaft MUSTERWEG   7", liste) == ["Verbotsliste: Privatadresse"]
    assert finde_im_text("Leitung geheimprojekt kolibri", liste) == ["Verbotsliste"]
    assert finde_im_text("Musterweg 70", liste) == []


def test_erlaubt_unterdrueckt_bekannten_fehlalarm(tmp_path):
    liste = lade_verbotsliste(_liste(tmp_path, f"erlaubt:\n  - '{IBAN}'\n"))
    assert finde_im_text(f"Spendenkonto der Firma: {IBAN}", liste) == []


def test_kaputte_verbotsliste_bricht_ab(tmp_path):
    with pytest.raises(ValueError):
        lade_verbotsliste(_liste(tmp_path, "verboten: [unvollstaendig"))
    with pytest.raises(ValueError):
        lade_verbotsliste(_liste(tmp_path, "verboten:\n  - abc\n"))


def test_fehlende_verbotsliste_heisst_nur_feste_muster(tmp_path):
    liste = lade_verbotsliste(tmp_path / "gibt_es_nicht.yaml")
    assert liste.eintraege == ()


def test_verbotsliste_ueber_umgebungsvariable(monkeypatch, tmp_path):
    p = _liste(tmp_path, "verboten:\n  - Kolibri-Intern\n")
    monkeypatch.setenv("CV_TAILOR_AUSGANGSSPERRE_LISTE", str(p))
    assert pruefe_nachrichten([{"role": "user", "content": "kolibri-intern"}])


def test_beispieldatei_ist_gueltig():
    beispiel = ROOT / "data" / "ausgangssperre.example.yaml"
    liste = lade_verbotsliste(beispiel)
    assert liste.eintraege, "Beispieldatei soll mindestens einen Eintrag zeigen"


def test_lokale_verbotsliste_ist_gitignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/ausgangssperre.local.yaml" in gitignore


# ---------------------------------------------------------------------------
# Sperre und Fehlermeldung
# ---------------------------------------------------------------------------

def test_fehlermeldung_nennt_art_nie_den_wert():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "System"}]},
        {"role": "user", "content": f"AHV {AHV}, Konto {IBAN}"},
    ]
    with pytest.raises(AusgangGesperrt) as info:
        sperre_pruefen(messages, agent="writer")
    meldung = str(info.value)
    assert "AHV-Nummer" in meldung and "IBAN" in meldung and "Nachricht 1 (user)" in meldung
    assert AHV not in meldung and "1234" not in meldung and "0076" not in meldung


def test_content_arrays_werden_geprueft():
    messages = [{"role": "user", "content": [{"type": "text", "text": f"Karte {KARTE}"}]}]
    assert [f.art for f in pruefe_nachrichten(messages)] == ["Kreditkartennummer"]


def _config(tmp_path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text("agents:\n  writer: { provider: openai, model: gpt-4.1 }\n", encoding="utf-8")
    return config


def test_call_llm_sendet_nichts_bei_fund(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "_config_cache", {})
    called = []
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: called.append(kw))

    with pytest.raises(AusgangGesperrt):
        llm.call_llm(
            agent="writer",
            phase="test",
            run_id="X",
            messages=[{"role": "user", "content": f"Bitte CV schreiben. AHV {AHV}"}],
            config_path=config,
        )
    assert called == []
    log_text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*/*.jsonl"))
    assert "AusgangGesperrt" in log_text and AHV not in log_text


def test_call_llm_laesst_sauberen_prompt_durch(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "_config_cache", {})
    monkeypatch.setattr(llm, "_cost_limit_cfg_cache", {})
    monkeypatch.delenv("CV_TAILOR_DAILY_COST_LIMIT_USD", raising=False)
    resp = SimpleNamespace(
        usage=None,
        _hidden_params={"response_cost": 0.0},
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))],
    )
    monkeypatch.setattr(llm.litellm, "completion", lambda **kw: resp)
    out = llm.call_llm(
        agent="writer",
        phase="test",
        run_id="X",
        messages=[{"role": "user", "content": "Alex Müller, Product Manager bei DemoSaaS"}],
        config_path=config,
    )
    assert out == "ok"


# ---------------------------------------------------------------------------
# Einziger Engpass: kein Anbieter-Client ausserhalb von llm.py
# ---------------------------------------------------------------------------

_ANBIETER = {"litellm", "anthropic", "openai", "google", "mistralai", "cohere", "ollama"}
_ERLAUBT = {Path("src/cv_tailor/llm.py")}


def _importierte_module(pfad: Path) -> set[str]:
    baum = ast.parse(pfad.read_text(encoding="utf-8"))
    namen: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            namen.update(a.name.split(".")[0] for a in knoten.names)
        elif isinstance(knoten, ast.ImportFrom) and knoten.module and knoten.level == 0:
            namen.add(knoten.module.split(".")[0])
        elif (
            isinstance(knoten, ast.Call)
            and getattr(knoten.func, "attr", getattr(knoten.func, "id", "")) in {
                "import_module", "__import__"
            }
            and knoten.args
            and isinstance(knoten.args[0], ast.Constant)
            and isinstance(knoten.args[0].value, str)
        ):
            namen.add(knoten.args[0].value.split(".")[0])
    return namen


def test_kein_anbieter_import_ausserhalb_von_llm_py():
    dateien = [
        p for ordner in ("src", "scripts", "evals") if (ROOT / ordner).exists()
        for p in (ROOT / ordner).rglob("*.py")
    ]
    assert dateien
    verstoesse = []
    for p in dateien:
        rel = p.relative_to(ROOT)
        if rel in _ERLAUBT or ".venv" in rel.parts:
            continue
        treffer = _importierte_module(p) & _ANBIETER
        if treffer:
            verstoesse.append(f"{rel}: {sorted(treffer)}")
    assert not verstoesse, (
        "Modellaufrufe muessen durch llm.call_llm (Ausgangssperre) laufen: " + "; ".join(verstoesse)
    )


def test_call_llm_prueft_vor_dem_senden():
    """Die Sperre steht im Code vor litellm.completion, nicht danach."""
    quelle = (ROOT / "src" / "cv_tailor" / "llm.py").read_text(encoding="utf-8")
    assert 0 < quelle.index("sperre_pruefen(messages") < quelle.index("litellm.completion(**")


# ---------------------------------------------------------------------------
# Datensparsamkeit an der Quelle
# ---------------------------------------------------------------------------

CV = """# Alex Müller

**Adresse:** Musterweg 7, 8000 Zürich
**Mobile:** +41 79 000 11 22
**Geburtsdatum:** 01. Januar 1985

---

## Management Summary

**Fokus:** Produktstrategie
Product Leader mit 10 Jahren Erfahrung.
"""


def test_ohne_kontaktblock_entfernt_nur_den_kopf():
    out = ohne_kontaktblock(CV)
    assert "Musterweg" not in out and "Geburtsdatum" not in out and "+41" not in out
    assert out.startswith("# Alex Müller")
    assert "**Fokus:** Produktstrategie" in out  # Inhalte unter ## bleiben
    assert finde_im_text(out) == []


@pytest.mark.parametrize("lang", ["de", "en"])
def test_persona_standard_cv_ohne_kopf_ist_sauber(lang):
    """Die Demo-Persona schreibt ein Geburtsdatum in den Kopf; ohne Kopf ist es weg."""
    import importlib.util

    import yaml

    skript = ROOT / "scripts" / "init_persona.py"
    persona_pfad = ROOT / "data" / "persona.yaml"
    spec = importlib.util.spec_from_file_location("init_persona", skript)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    daten = yaml.safe_load(persona_pfad.read_text(encoding="utf-8"))
    persona = daten.get("persona", daten)
    cv = mod.render_standard_cv(persona, lang)
    assert "Geburtsdatum" in finde_im_text(cv)
    assert finde_im_text(ohne_kontaktblock(cv)) == []


def test_ohne_geburtsdatum_behaelt_den_beleg():
    satz = "Herr Muster, geboren am 3. April 1980, war von 2015 bis 2020 als Product Owner tätig."
    out = ohne_geburtsdatum(satz)
    assert GEBURTSDATUM_ERSATZ in out and "1980" not in out
    assert "von 2015 bis 2020 als Product Owner" in out


def _index() -> dict:
    return {"entries": [
        {"id": "BELG-001", "typ": "other", "quelle_typ": "standard_cv",
         "quelle_position": "line:3", "quelle_datei": "data/standard_cv.md",
         "snippet": "**Adresse:** Musterweg 7, 8000 Zürich"},
        {"id": "BELG-002", "typ": "other", "quelle_typ": "standard_cv",
         "quelle_position": "line:5", "quelle_datei": "data/standard_cv.md",
         "snippet": "**Geburtsdatum:** 01. Januar 1985"},
        {"id": "BELG-003", "typ": "role", "quelle_typ": "zeugnis",
         "quelle_position": "p1", "quelle_datei": "data/zeugnisse/demo.pdf",
         "snippet": "Herr Muster, geboren am 3.4.1980, leitete als Product Owner das Team."},
        {"id": "BELG-004", "typ": "achievement", "quelle_typ": "standard_cv",
         "quelle_position": "line:20", "quelle_datei": "data/standard_cv.md",
         "snippet": "Team von 12 Personen als Product Owner geführt."},
    ]}


def test_beleg_index_kompakt_ohne_kontakt_und_geburtsdatum():
    out = format_beleg_index_compact(_index())
    assert "BELG-001" not in out and "BELG-002" not in out
    assert "BELG-003" in out and "BELG-004" in out
    assert finde_im_text(out) == []


def test_experience_activation_ohne_kontakt_und_geburtsdatum():
    anzeige = "Product Owner mit Teamführung und Stakeholder-Management"
    aktiviert = activate_entries(anzeige, _index())
    alle = [e for liste in aktiviert.values() for e in liste]
    assert "BELG-003" in {e.id for e in alle}  # Beleg bleibt, nur das Datum faellt weg
    assert all(e.id not in {"BELG-001", "BELG-002"} for e in alle)
    assert finde_im_text(json.dumps([e.snippet for e in alle], ensure_ascii=False)) == []


# ---------------------------------------------------------------------------
# Luecken aus dem Review 2026-11-23 (jeder Fall war mit dem alten Code gruen
# durchgelassen bzw. still ohne Liste weitergelaufen)
# ---------------------------------------------------------------------------

# Offizielle Beispiel-IBAN (DE) und Amex-Testkarte, zur Laufzeit zusammengesetzt.
IBAN_DE = "DE89 3704 " + "0044 0532 0130 00"
KARTE_AMEX = "3782 822463 " + "10005"
NBSP, SCHMALES_NBSP, NULLBREITE = " ", " ", "​"
VOLLBREIT = {c: c + 0xFEE0 for c in range(0x21, 0x7F)}


@pytest.mark.parametrize(
    ("text", "art"),
    [
        (f"Konto {IBAN} BIC UBSWCHZH", "IBAN"),
        (f"Konto {IBAN} 8000 Zuerich", "IBAN"),
        (f"Konto {IBAN_KOMPAKT} BIC UBSWCHZH", "IBAN"),
        (f"IBAN {IBAN_DE} BIC COBADEFFXXX", "IBAN"),
        (f"Karte {KARTE} 12 27 CVC 123", "Kreditkartennummer"),
        (f"Karte {KARTE} 123", "Kreditkartennummer"),
        (f"Karte {KARTE_AMEX} 0427", "Kreditkartennummer"),
        (f"Kartennummer {KARTE}.", "Kreditkartennummer"),
    ],
)
def test_nachfolgender_text_versteckt_iban_und_karte_nicht(text, art):
    assert art in finde_im_text(text)


@pytest.mark.parametrize(
    ("text", "art"),
    [
        (f"Konto {IBAN.replace(' ', NBSP)}", "IBAN"),
        (f"Konto {IBAN.replace(' ', SCHMALES_NBSP)}", "IBAN"),
        (f"Konto {IBAN.replace(' ', NULLBREITE + ' ')}", "IBAN"),
        (f"Konto {IBAN.lower()}", "IBAN"),
        (f"Konto {IBAN_DE.lower()} bic cobadeffxxx", "IBAN"),
        (f"Konto {IBAN_KOMPAKT.translate(VOLLBREIT)}", "IBAN"),
        (f"Karte {KARTE.replace(' ', NBSP)}", "Kreditkartennummer"),
        (f"Karte {KARTE.replace(' ', chr(9))}", "Kreditkartennummer"),
        ("AHV " + AHV.replace(".", NBSP), "AHV-Nummer"),
    ],
)
def test_unicode_leerzeichen_und_kleinschreibung_verstecken_nichts(text, art):
    assert art in finde_im_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "Rechnung 4111 1111 1111 1112 vom 12.03.2024",
        "FY24 Budget fuer Projekt Alpha und Beta im Team von zwoelf",
        f"Konto {IBAN_FALSCHE_PRUEFZIFFER} BIC UBSWCHZH",
        "Summe 4111111111111111.50 laut Tabelle",
        f"Telefon{NBSP}+41{NBSP}44{NBSP}555{NBSP}01{NBSP}23",
    ],
)
def test_keine_neuen_fehlalarme(text):
    assert finde_im_text(text) == []


def test_erlaubt_greift_auch_bei_geschuetztem_leerzeichen(tmp_path):
    liste = lade_verbotsliste(_liste(tmp_path, f"erlaubt:\n  - '{IBAN}'\n"))
    assert finde_im_text(f"Spendenkonto: {IBAN.replace(' ', NBSP)} BIC UBSWCHZH", liste) == []


def test_meldung_nennt_auch_bei_neuen_funden_keinen_wert():
    messages = [{"role": "user", "content": f"Konto {IBAN} BIC UBSWCHZH, Karte {KARTE} 123"}]
    with pytest.raises(AusgangGesperrt) as info:
        sperre_pruefen(messages, liste=lade_verbotsliste(Path("gibt-es-nicht.yaml")))
    meldung = str(info.value)
    assert "IBAN" in meldung and "Kreditkartennummer" in meldung
    for teil in ("0076", "5295", "1111", "UBSWCHZH"):
        assert teil not in meldung


def test_gesetzte_variable_ohne_datei_bricht_ab(monkeypatch, tmp_path):
    fehlt = tmp_path / "vertippt.yaml"
    monkeypatch.setenv(ausgangssperre.VERBOTSLISTE_ENV, str(fehlt))
    with pytest.raises(ValueError, match=ausgangssperre.VERBOTSLISTE_ENV):
        lade_verbotsliste()
    with pytest.raises(ValueError):
        sperre_pruefen([{"role": "user", "content": "harmloser Text"}])


def test_standardliste_liegt_relativ_zum_repo():
    assert ausgangssperre.VERBOTSLISTE_PFAD.is_absolute()
    assert ausgangssperre.VERBOTSLISTE_PFAD == ROOT_VERBOTSLISTE


def test_fehlende_standardliste_steht_im_log(monkeypatch, tmp_path):
    from structlog.testing import capture_logs

    monkeypatch.delenv(ausgangssperre.VERBOTSLISTE_ENV, raising=False)
    monkeypatch.setattr(ausgangssperre, "VERBOTSLISTE_PFAD", tmp_path / "fehlt.yaml")
    monkeypatch.setattr(ausgangssperre, "_HINWEIS_GEGEBEN", False)
    with capture_logs() as logs:
        assert lade_verbotsliste().eintraege == ()
        lade_verbotsliste()
    hinweise = [e for e in logs if e["event"] == "ausgangssperre.ohne_verbotsliste"]
    assert len(hinweise) == 1


# Gemeldet 2026-11-27: Nummern mit «–» statt «-» kamen an allen Mustern vorbei.
STRICHE = ["–", "—", "‑", "‐", "−", "‒", "―", "⁃", "﹣"]


@pytest.mark.parametrize("strich", STRICHE)
@pytest.mark.parametrize(
    "vorlage, art",
    [
        ("AHV 756{s}1234{s}5678{s}97", "AHV-Nummer"),
        ("Konto CH93{s}0076{s}2011{s}6238{s}5295{s}7", "IBAN"),
        ("Karte 4111{s}1111{s}1111{s}1111", "Kreditkartennummer"),
        ("geboren am 1980{s}01{s}02", "Geburtsdatum"),
    ],
)
def test_jeder_strich_trennt_wie_der_bindestrich(strich, vorlage, art):
    assert art in finde_im_text(vorlage.format(s=strich))


def test_verbotsliste_greift_auch_mit_halbgeviertstrich(tmp_path):
    eintrag = "verboten:\n  - { wert: '+41 79 000 11 22', art: Telefon privat }\n"
    liste = lade_verbotsliste(_liste(tmp_path, eintrag))
    assert finde_im_text("Mobile: +41–79–000–11–22", liste) == ["Verbotsliste: Telefon privat"]


def test_normalisierung_behaelt_die_laenge():
    text = "2019–2024 — Team‑Lead"
    assert len(ausgangssperre.normalisiere(text)) == len(text)
    assert ausgangssperre.normalisiere(text) == "2019-2024 - Team-Lead"
