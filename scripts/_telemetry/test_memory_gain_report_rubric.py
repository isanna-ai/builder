"""Report-metric tests for `rubric_score` in the memory gain report.

Asserts the per-mode and per-recall-mode group summaries expose mean+median
rubric_score, the deltas dict carries a `rubric_score` entry with a computed
Cohen's d / p-value at >=2 samples/arm, and the rendered markdown mentions the
rubric. Shim-compatible: bare `test_*`, plain asserts, no classes/marks.
"""

from __future__ import annotations

from _telemetry.memory_gain_report import (
    build_gain_report,
    group_by_mode,
    group_by_recall_mode,
    render_markdown,
)


def _rec(memory_mode: str, recall_mode: str, rubric_score: int, tokens_out: int = 100) -> dict:
    return {
        "artifact": "memory_eval",
        "memory_mode": memory_mode,
        "recall_mode": recall_mode,
        "rubric_score": rubric_score,
        "plan_tokens_out": tokens_out,
        "plan_wall_ms": 1000,
        "recall_calls": 0,
        "recall_hits": 0,
        "decisions_reused": 0,
        "prior_art_tokens": 0,
        "decisions_distilled": 0,
        "decisions_deduped": 0,
    }


def _records() -> list[dict]:
    # off arm: low rubric (40, 50); hivemind/pull arm: high rubric (80, 90).
    return [
        _rec("off", "off", 40),
        _rec("off", "off", 50),
        _rec("hivemind", "pull", 80),
        _rec("hivemind", "pull", 90),
    ]


def test_group_by_mode_exposes_rubric_mean_and_median():
    groups = group_by_mode(_records())
    assert "mean_rubric_score" in groups["off"]
    assert "median_rubric_score" in groups["off"]
    assert groups["off"]["mean_rubric_score"] == 45.0
    assert groups["off"]["median_rubric_score"] == 45.0
    assert groups["hivemind"]["mean_rubric_score"] == 85.0
    assert groups["hivemind"]["median_rubric_score"] == 85.0


def test_group_by_recall_mode_exposes_rubric_mean_and_median():
    groups = group_by_recall_mode(_records())
    assert "mean_rubric_score" in groups["off"]
    assert "median_rubric_score" in groups["off"]
    assert "mean_rubric_score" in groups["pull"]
    assert groups["pull"]["mean_rubric_score"] == 85.0


def test_build_gain_report_has_rubric_delta_with_significance():
    report = build_gain_report(_records())
    assert "rubric_score" in report["deltas"]
    d = report["deltas"]["rubric_score"]
    assert d["off_mean"] == 45.0
    assert d["hivemind_mean"] == 85.0
    assert d["abs_delta"] == 40.0
    # >= 2 samples/arm -> Cohen's d and p-value are computed (not None).
    assert d["cohens_d"] is not None
    assert d["p_value"] is not None


def test_build_gain_report_rubric_significance_null_below_two_samples():
    # 1 sample/arm -> Cohen's d and p-value are None (never fabricated).
    records = [_rec("off", "off", 40), _rec("hivemind", "pull", 90)]
    report = build_gain_report(records)
    d = report["deltas"]["rubric_score"]
    assert d["cohens_d"] is None
    assert d["p_value"] is None


def test_render_markdown_mentions_rubric():
    report = build_gain_report(_records())
    md = render_markdown(report)
    assert "rubric" in md
    # The per-mode table renders the mean rubric column with the off arm's value.
    assert "rubric_score" in md


def test_render_markdown_still_renders_legacy_metrics():
    # The additive rubric column does not displace the existing significance rows.
    report = build_gain_report(_records())
    md = render_markdown(report)
    assert "plan_tokens_out" in md
    assert "plan_wall_ms" in md
    assert "Memory Gain Report" in md


def test_rubric_delta_excludes_unscored_records():
    # Records with rubric_score=0 are the "not evaluated" sentinel; they must NOT
    # contribute to the rubric delta. Only scored (>0) records count.
    # off arm: 2 unscored (0), 2 scored (40, 60) -> scored off = [40, 60], mean 50
    # hivemind arm: 1 unscored (0), 2 scored (80, 90) -> scored hive = [80, 90], mean 85
    records = [
        _rec("off", "off", 0),   # unscored
        _rec("off", "off", 0),   # unscored
        _rec("off", "off", 40),  # scored
        _rec("off", "off", 60),  # scored
        _rec("hivemind", "push", 0),  # unscored
        _rec("hivemind", "push", 80), # scored
        _rec("hivemind", "push", 90), # scored
    ]
    report = build_gain_report(records)
    d = report["deltas"]["rubric_score"]
    # Only the 2 scored off records (40, 60) contribute -> mean 50
    assert d["off_mean"] == 50.0
    # Only the 2 scored hivemind records (80, 90) contribute -> mean 85
    assert d["hivemind_mean"] == 85.0
    # rubric_scored_count = 2 off + 2 hive = 4
    assert d["rubric_scored_count"] == 4
    # Unscored tokens_out records all contribute (separate metric, unchanged)
    tokens_d = report["deltas"]["plan_tokens_out"]
    assert "rubric_scored_count" not in tokens_d
