"""Tests for the reviewer-verdict parsing contract in writer_loop.

Pins the two contract fixes from 2026-08-14:
1. The HM prompt's actual reject verdict word "Ablegen" triggers a veto
   (previously only "Ablehnung"/"rejected" were parsed — 14 historical
   reviews said "Ablegen" without effect).
2. Only bold `**…Veto**` markers count as concrete coach vetos — the coach
   prompt now instructs this explicitly (Veto-Markierungs-Kontrakt).
"""
from cv_tailor.agents.writer_loop import (
    _count_explicit_veto_markers,
    _reviewer_signals_veto,
)


def test_ablegen_verdict_triggers_veto():
    review = (
        "**Soforteindruck:** Zu generisch.\n\n"
        "**Schwächen:** Kein Zahlenanker.\n\n"
        "**Gesamturteil:**\nAblegen."
    )
    assert _reviewer_signals_veto(review)


def test_ablehnung_still_triggers_veto():
    assert _reviewer_signals_veto("…\n**Gesamturteil:** Ablehnung — Substanz fehlt.")


def test_weiterleiten_does_not_trigger():
    review = (
        "**Stärken:** Klarer Aufbau.\n\n"
        "**Gesamturteil:**\nWeiterleiten (überzeugend)"
    )
    assert not _reviewer_signals_veto(review)


def test_weiterleiten_mit_fragezeichen_does_not_trigger():
    assert not _reviewer_signals_veto("**Gesamturteil:** Weiterleiten mit Fragezeichen")


def test_bold_veto_marker_triggers():
    review = (
        "## Rollen-Profil-Match\n"
        "**Veto: Anzeigen-Etikett** — 'als einziger PM' stammt aus der Anzeige.\n\n"
        "## Gesamturteil\nÜberarbeitung nötig"
    )
    assert _reviewer_signals_veto(review)
    assert _count_explicit_veto_markers(review) == 1


def test_unformatted_veto_does_not_trigger():
    """Documents the contract: unbolded vetos are NOT enforced — the coach
    prompt instructs bold markers for exactly this reason."""
    review = (
        "Das ist ein Veto. Diese Formulierung geht nicht.\n\n"
        "## Gesamturteil\nÜberarbeitung nötig"
    )
    assert not _reviewer_signals_veto(review)


def test_ueberarbeitung_noetig_alone_is_advisory():
    assert not _reviewer_signals_veto("## Gesamturteil\nÜberarbeitung nötig")


def test_grundsaetzliches_problem_triggers():
    assert _reviewer_signals_veto("## Gesamturteil\nGrundsätzliches Problem")


def test_options_echo_does_not_trigger():
    """An echoed option-scale template line is not a verdict."""
    review = (
        "**Gesamturteil:** Weiterleiten (überzeugend) / "
        "Weiterleiten mit Fragezeichen / Ablegen → Weiterleiten (überzeugend)"
    )
    assert not _reviewer_signals_veto(review)


def test_negated_ablegen_does_not_trigger():
    review = "**Gesamturteil:** Weiterleiten — ich würde das Dossier nicht ablegen."
    assert not _reviewer_signals_veto(review)


def test_negated_grundsaetzliches_problem_does_not_trigger():
    review = "## Gesamturteil\nÜberarbeitung nötig — aber kein grundsätzliches Problem."
    assert not _reviewer_signals_veto(review)


def test_ablegen_in_body_prose_does_not_trigger():
    """'ablegen' is an ordinary German verb — only the Gesamturteil block counts."""
    review = (
        "Der Entwurf sollte die Floskel ablegen und präziser werden. "
        "Auch Rechenschaft ablegen ist kein CV-Vokabular.\n\n"
        "## Gesamturteil\nBereit"
    )
    assert not _reviewer_signals_veto(review)


def test_allcaps_veto_marker_counts():
    review = "**VETO: Anzeigen-Etikett** — Wortlaut stammt aus der Anzeige.\n\n## Gesamturteil\nÜberarbeitung nötig"
    assert _count_explicit_veto_markers(review) == 1
    assert _reviewer_signals_veto(review)


def test_bold_kein_veto_is_not_a_marker():
    review = "**Kein Veto** — alle Claims belegt.\n\n## Gesamturteil\nBereit"
    assert _count_explicit_veto_markers(review) == 0
    assert not _reviewer_signals_veto(review)


def test_veto_with_negation_in_reason_still_counts():
    """Negation after the veto word ('nicht belegt' as reason) stays a veto."""
    review = "**Veto: Adressat nicht belegt** — Nutzergruppe stammt nicht aus dem Beleg."
    assert _count_explicit_veto_markers(review) == 1
