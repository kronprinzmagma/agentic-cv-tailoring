"""Quellen und Hinweise des Sektions-Faktenchecks (2026-11-25)."""
import json

from cv_tailor import factcheck_sources as fs
from cv_tailor.agents import factcheck as fc
from cv_tailor.quality_snapshot import _count_factcheck_vetos
from tests.test_factcheck_truncation import _FakeLLM, run_ctx  # noqa: F401
from tests.test_factcheck_verdict import iteration_env  # noqa: F401


def test_standard_cv_without_contact_block_and_demoted(tmp_path):
    path = tmp_path / "cv.md"
    path.write_text(
        "# Demo\n\n**E-Mail:** demo@example.org\n\n---\n\n## Summary\nText\n### 2020 | Firma\n",
        encoding="utf-8",
    )
    block = fs.standard_cv_block(path)
    assert "demo@example.org" not in block
    assert "\n#### Summary" in block and "\n##### 2020 | Firma" in block
    assert fs.standard_cv_block(tmp_path / "fehlt.md") == ""


def test_portfolio_only_seen_entries_never_gaps(tmp_path):
    def entry(i, **f):
        return {"id": f"BELG-{i}", "typ": "skill", "snippet": f"s{i}", "quelle_position": "p",
                "quelle_typ": "portfolio", "section": "Tools", **f}

    index = tmp_path / "index.json"
    index.write_text(json.dumps({"entries": [
        entry(1), entry(2, section="Wachstumskanten"), entry(3), entry(4, quelle_typ="zeugnis"),
        entry(10),
    ]}), encoding="utf-8")
    (tmp_path / "01_analyse.md").write_text("BELG-10", encoding="utf-8")
    (tmp_path / "_experience_activation.md").write_text("BELG-2 BELG-3 BELG-4", encoding="utf-8")
    block = fs.portfolio_block(tmp_path, index)
    assert "BELG-3 " in block and "BELG-10 " in block
    assert "BELG-1 " not in block and "BELG-2 " not in block and "BELG-4 " not in block


def test_extra_clarifications_need_posting_and_matching_section(tmp_path, monkeypatch):
    from cv_tailor.clarifications import EXCLUDED_BUDGET, EXCLUDED_TOPIC, ClarificationSelection

    words = "Produktstrategie Priorisierung Plattform Migration Stakeholder Einführung"
    selection = ClarificationSelection(
        entries=[{"answers_markdown": words}, {"answers_markdown": "Ganz anderes"},
                 {"answers_markdown": words + " Themenfremd"}],
        current_topics=set(), included=[],
        excluded=[(0, EXCLUDED_BUDGET), (1, EXCLUDED_BUDGET), (2, EXCLUDED_TOPIC)],
        used_chars=0, max_chars=1,
    )
    monkeypatch.setattr(fs, "select_clarifications", lambda *a, **k: selection)
    block = fs.extra_clarifications_block(words, "Anzeige")
    assert "Produktstrategie" in block and "Themenfremd" not in block and "Ganz anderes" not in block
    assert fs.extra_clarifications_block(words, None) == ""


def _answer(grade):
    return json.dumps({"veto": grade != "hinweis", "has_gaps": False, "geprueft": ["Text"], "probleme": [
        {"behauptung": "Stelle", "art": "sonstiges", "beleg_ids": [], "begruendung": "g",
         "einstufung": grade}], "findings_markdown": "Prosa"})


def test_hints_are_visible_but_not_for_the_writer(run_ctx, iteration_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fc, "call_llm", _FakeLLM([(_answer("hinweis"), False)]))
    assert fc.run_factcheck_iteration(run_ctx, "management_summary", "Text", 1) is False
    iter_dir = run_ctx.run_dir / "03_iterationen"
    assert "Stelle" not in (iter_dir / "management_summary_v1_factcheck.md").read_text()
    assert "Stelle" in (run_ctx.run_dir / fc.HINTS_FILENAME).read_text()
    assert _count_factcheck_vetos(iter_dir) == 0


def test_blocking_round_clears_old_hints_and_counts(run_ctx, iteration_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fc, "call_llm", _FakeLLM(
        [(_answer("hinweis"), False), (_answer("kein_beleg"), False)]))
    fc.run_factcheck_iteration(run_ctx, "management_summary", "Text", 1)
    assert fc.run_factcheck_iteration(run_ctx, "management_summary", "Text", 2) is True
    assert not (run_ctx.run_dir / fc.HINTS_FILENAME).exists()
    assert _count_factcheck_vetos(run_ctx.run_dir / "03_iterationen") == 1


def test_veto_without_passage_is_repeated_then_veto(run_ctx, iteration_env, monkeypatch):  # noqa: F811
    bad = json.dumps({"veto": True, "has_gaps": False, "probleme": []})
    llm = _FakeLLM([(bad, False), (bad, False)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck_iteration(run_ctx, "management_summary", "Text", 1) is True
    assert len(llm.max_tokens_seen) == 2
