from __future__ import annotations

from _telemetry.memory_gain_report import (
    build_gain_report,
    cohens_d,
    group_by_mode,
    mann_whitney_u,
    render_markdown,
)
from _telemetry.memory_eval import build_memory_eval


def _rec(mode: str, tokens_out: int, wall_ms: int, recall_calls: int = 0, recall_hits: int = 0, decisions_reused: int = 0):
    return build_memory_eval(
        run_id="w",
        spec_id="s",
        lane="claude",
        plan_tokens_in=100,
        plan_tokens_out=tokens_out,
        plan_wall_ms=wall_ms,
        spec_outcome="unknown",
        memory_mode=mode,
        recall_calls=recall_calls,
        recall_hits=recall_hits,
        decisions_reused=decisions_reused,
    )


def _off_arm():
    return [_rec("off", t, t * 10) for t in (1000, 1100, 1050, 1200, 980)]


def _hivemind_arm():
    return [_rec("hivemind", t, t * 10, recall_calls=1, recall_hits=1, decisions_reused=2) for t in (600, 650, 620, 700, 580)]


def test_group_by_mode_fields():
    records = _off_arm() + _hivemind_arm()
    groups = group_by_mode(records)
    assert groups["off"]["specs_completed"] == 5
    assert groups["off"]["mean_plan_tokens_out"] > groups["hivemind"]["mean_plan_tokens_out"]
    assert "median_plan_tokens_out" in groups["off"]
    assert "mean_plan_wall_ms" in groups["off"]
    assert "mean_decisions_reused" in groups["off"]


def test_recall_hit_rate_zero_denominator():
    groups = group_by_mode(_off_arm())
    assert groups["off"]["recall_hit_rate"] == 0.0


def test_recall_hit_rate_with_hits():
    groups = group_by_mode(_hivemind_arm())
    assert groups["hivemind"]["recall_hit_rate"] == 1.0


def test_cohens_d_sign_and_finite():
    off = [1000.0, 1100.0, 1050.0, 1200.0, 980.0]
    hive = [600.0, 650.0, 620.0, 700.0, 580.0]
    d = cohens_d(off, hive)
    assert d is not None
    assert d == d  # not NaN
    assert abs(d) < float("inf")
    assert d > 0  # off mean > hive mean


def test_mann_whitney_p_in_range():
    off = [1000.0, 1100.0, 1050.0, 1200.0, 980.0]
    hive = [600.0, 650.0, 620.0, 700.0, 580.0]
    p = mann_whitney_u(off, hive)
    assert p is not None
    assert 0.0 <= p <= 1.0


def test_small_arm_yields_none():
    assert cohens_d([1.0], [2.0, 3.0]) is None
    assert mann_whitney_u([1.0], [2.0, 3.0]) is None


def test_build_and_render():
    report = build_gain_report(_off_arm() + _hivemind_arm())
    assert "groups" in report
    assert "deltas" in report
    md = render_markdown(report)
    assert "off" in md
    assert "Cohen" in md
    assert "Mann-Whitney" in md
