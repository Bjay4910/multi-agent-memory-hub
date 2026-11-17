"""Regression guard for the recall evaluation (no LLM calls)."""

from room.memory.evaluation import default_eval_dir, evaluate


def test_fixture_has_about_25_questions():
    import json

    qs = json.loads((default_eval_dir() / "questions.json").read_text())
    assert 20 <= len(qs) <= 30
    assert sum("stale_text" in q for q in qs) >= 3


def test_eval_metrics_meet_floor():
    report = evaluate()
    assert report.questions == 25
    assert report.stale_checks >= 3 and report.stale_violations == 0 and report.current_missing == 0
    assert report.recall_at_8 >= 0.85
    assert report.recall_at_3 >= 0.75
    assert 0 < report.precision_at_3 <= report.precision_at_3_max <= 1
    text = report.format(verbose=True)
    assert "recall@3" in text and "PASS" in text
