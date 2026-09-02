"""Opt-in independent-review pipeline: pre-plan spec review + post-hoc adversarial
review (codex/gpt-5.4 reviewer) + a claude fix pass, all gated behind
pipeline.reviews.enabled. When disabled, the 4-phase order is byte-identical."""

from __future__ import annotations

from pathlib import Path

from _dispatch_runtime import phase_runtime as pr
from _dispatch_runtime.phase_routing import PHASE_CLASS_MAP, REVIEW_LANE_PHASES
from _dispatch_runtime.config import load_dispatch_config
from _dispatch_runtime.scheduler import DispatchScheduler
from _validators.legacy import validate_spec_yaml


def _walk(start: str) -> list[str]:
    seq, p = [], start
    while p:
        seq.append(p)
        p = pr.next_phase(p)
    return seq


def test_reviews_off_is_identical_four_phase():
    pr.set_active_phase_order(pr.effective_phase_order(False))
    try:
        assert _walk("spec") == ["spec", "plan", "implement", "verify", "sync"]
        assert pr.next_phase("verify") == "sync"
    finally:
        pr.set_active_phase_order(pr.SPEC_PHASE_ORDER)


def test_reviews_on_inserts_review_phases_in_order():
    pr.set_active_phase_order(pr.effective_phase_order(True))
    try:
        assert _walk("spec") == [
            "spec", "spec-review", "plan", "implement",
            "adversarial-review", "review-fix", "verify", "sync",
        ]
        assert pr.next_phase("sync") is None
    finally:
        pr.set_active_phase_order(pr.SPEC_PHASE_ORDER)


def test_review_phase_output_statuses_are_validated(tmp_path: Path):
    for status, current_phase in (
        ("spec-reviewed", "plan"),
        ("adversarially-reviewed", "review-fix"),
    ):
        spec_file = tmp_path / f"{status}.yaml"
        spec_file.write_text(
            "name: demo\n"
            "created: 2026-07-20\n"
            f"status: {status}\n"
            f"current_phase: {current_phase}\n"
            "next_action: continue\n",
            encoding="utf-8",
        )
        assert validate_spec_yaml(spec_file, contract_path=None, strict=True) == []


def test_per_spec_review_counts_select_orders_and_keep_verify():
    zero = pr.phase_order_for_count(pr.review_count_for_spec({"reviews": 0}, {"reviews": {"default": 1}}))
    one = pr.phase_order_for_count(pr.review_count_for_spec({"reviews": 1}, {"reviews": {"default": 0}}))
    two = pr.phase_order_for_count(pr.review_count_for_spec({"reviews": 2}, {"reviews": {"default": 0}}))
    assert zero == ["spec", "plan", "implement", "verify", "sync"]
    assert all(phase not in zero for phase in ("spec-review", "adversarial-review", "review-fix"))
    assert one == pr.REVIEW_SPEC_PHASE_ORDER
    assert two == [
        "spec", "spec-review", "spec-review-2", "plan", "implement",
        "adversarial-review", "adversarial-review-2", "review-fix", "verify", "sync",
    ]
    assert two[-1] == "sync"
    assert "verify" in zero and "verify" in one


def test_count_two_next_phase_doubles_each_review_gate():
    order = pr.phase_order_for_count(2)
    assert pr.next_phase("spec-review", order=order) == "spec-review-2"
    assert pr.next_phase("adversarial-review", order=order) == "adversarial-review-2"
    assert pr.next_phase("adversarial-review-2", order=order) == "review-fix"


def test_unset_reviews_uses_dispatcher_default_and_legacy_enabled():
    assert pr.review_count_for_spec({}, {"reviews": {"default": 1}}) == 1
    assert pr.review_count_for_spec({}, {"reviews": {"default": 0}}) == 0
    assert pr.review_count_for_spec({}, {"reviews": {"enabled": True}}) == 1
    assert pr.review_count_for_spec({}, {"reviews": {"enabled": False}}) == 0


def test_next_phase_explicit_order_does_not_depend_on_active_global():
    pr.set_active_phase_order(pr.REVIEW_SPEC_PHASE_ORDER)
    try:
        assert pr.next_phase("implement", order=pr.SPEC_PHASE_ORDER) == "verify"
        assert pr.next_phase("implement", order=pr.REVIEW_SPEC_PHASE_ORDER) == "adversarial-review"
        assert pr.next_phase("implement") == "adversarial-review"
    finally:
        pr.set_active_phase_order(pr.SPEC_PHASE_ORDER)


def test_completion_requirements_uses_the_spec_review_count(tmp_path: Path):
    specs = tmp_path / ".builder" / "specs"
    spec_dir = specs / "demo"
    spec_dir.mkdir(parents=True)
    (tmp_path / ".builder" / "dispatch.yaml").write_text(
        "pipeline:\n  reviews:\n    enabled: true\n", encoding="utf-8",
    )
    (spec_dir / "spec.yaml").write_text("reviews: 0\n", encoding="utf-8")
    assert "next_phase: verify" in pr._completion_requirements(specs, "demo", "implement")

    (spec_dir / "spec.yaml").write_text("reviews: 1\n", encoding="utf-8")
    assert "next_phase: adversarial-review" in pr._completion_requirements(specs, "demo", "implement")

    (spec_dir / "spec.yaml").write_text("reviews: 2\n", encoding="utf-8")
    assert "next_phase: spec-review-2" in pr._completion_requirements(specs, "demo", "spec-review")


def test_capability_and_lane_mapping():
    # Reviews run as independent_reviewer (-> gpt-5.4 on codex); fix is fast_editor.
    assert PHASE_CLASS_MAP["spec-review"] == "independent_reviewer"
    assert PHASE_CLASS_MAP["adversarial-review"] == "independent_reviewer"
    assert PHASE_CLASS_MAP["spec-review-2"] == "independent_reviewer"
    assert PHASE_CLASS_MAP["adversarial-review-2"] == "independent_reviewer"
    assert PHASE_CLASS_MAP["review-fix"] == "fast_editor"
    assert REVIEW_LANE_PHASES == frozenset({
        "spec-review", "spec-review-2", "adversarial-review", "adversarial-review-2",
    })


def test_scheduler_routes_codex_authored_work_to_cross_family_review(tmp_path: Path):
    cfg = load_dispatch_config(_write_cfg(
        tmp_path,
        "  - name: codex\n"
        "    provider: codex-cli\n"
        "    max_concurrency: 1\n"
        "pipeline:\n"
        "  default_lane: codex\n"
        "  reviews:\n"
        "    enabled: true\n"
        "    lane: codex\n",
    ))
    scheduler = DispatchScheduler.__new__(DispatchScheduler)
    scheduler.config = cfg
    scheduler.pipeline = cfg.pipeline
    scheduler._review_lane = "codex"
    assert scheduler._route_phase("adversarial-review", "codex") == "claude"


def test_review_phases_are_review_classified_except_fix():
    assert "spec-review" in pr.REVIEW_PHASES
    assert "spec-review-2" in pr.REVIEW_PHASES
    assert "adversarial-review" in pr.REVIEW_PHASES
    assert "adversarial-review-2" in pr.REVIEW_PHASES
    assert "review-fix" not in pr.REVIEW_PHASES  # the fix pass is not a review


def test_second_review_phases_require_distinct_proof_artifacts():
    assert pr.required_phase_artifact_groups("spec-review-2") != pr.required_phase_artifact_groups("spec-review")
    assert pr.required_phase_artifact_groups("adversarial-review-2") != pr.required_phase_artifact_groups("adversarial-review")


def test_gate_phase_sets():
    assert "spec-review" in pr.PRE_IMPLEMENT_PHASES
    assert "spec-review-2" in pr.PRE_IMPLEMENT_PHASES
    assert "adversarial-review" in pr.POST_GATE_PHASES
    assert "adversarial-review-2" in pr.POST_GATE_PHASES
    assert "review-fix" in pr.POST_GATE_PHASES


def test_build_phase_goal_directives(tmp_path: Path):
    specs = tmp_path / ".builder" / "specs"
    (specs / "demo").mkdir(parents=True)
    pr.set_active_phase_order(pr.effective_phase_order(True))
    try:
        sr = pr.build_phase_goal(tmp_path, specs, "demo", "spec-review", None)
        assert "SPEC-REVIEW" in sr and "INDEPENDENT" in sr and "review-log.yaml" in sr
        assert "REVIEW AUTO-APPLICATION" in sr
        assert "Do NOT rewrite the spec" not in sr

        ar = pr.build_phase_goal(tmp_path, specs, "demo", "adversarial-review", None)
        assert "ADVERSARIAL-REVIEW" in ar and "REVIEW AUTO-APPLICATION" in ar
        assert "Do NOT fix anything here" not in ar

        ar2 = pr.build_phase_goal(tmp_path, specs, "demo", "adversarial-review-2", None)
        assert "COMPLEMENTARY" in ar2 and "review-log-2.yaml" in ar2
        assert "Do NOT read or defer to review-log.yaml" in ar2

        rf = pr.build_phase_goal(tmp_path, specs, "demo", "review-fix", None)
        assert "REVIEW-FIX" in rf and "APPLY the confirmed findings" in rf
        assert "review-log.yaml and review-log-2.yaml" in rf
    finally:
        pr.set_active_phase_order(pr.SPEC_PHASE_ORDER)


def _write_cfg(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "dispatch.yaml"
    (tmp_path / ".builder" / "dispatch-queue").mkdir(parents=True, exist_ok=True)
    p.write_text(
        "queue_store:\n"
        f"  path: {tmp_path}/.builder/dispatch-queue\n"
        "lanes:\n"
        "  - name: claude\n"
        "    provider: claude-code-cli\n"
        "    max_concurrency: 1\n" + extra,
        encoding="utf-8",
    )
    return p


def test_config_reviews_default_on_for_new(tmp_path: Path):
    cfg = load_dispatch_config(_write_cfg(tmp_path))
    assert cfg.pipeline["reviews"]["enabled"] is True
    assert cfg.pipeline["reviews"]["lane"] == "codex"
    assert cfg.pipeline["reviews"]["default"] == 1


def test_config_reviews_can_be_pinned_off(tmp_path: Path):
    cfg = load_dispatch_config(_write_cfg(tmp_path, "pipeline:\n  reviews:\n    enabled: false\n"))
    assert cfg.pipeline["reviews"]["enabled"] is False
    assert cfg.pipeline["reviews"]["default"] == 0


def test_config_reviews_can_default_to_two(tmp_path: Path):
    cfg = load_dispatch_config(_write_cfg(tmp_path, "pipeline:\n  reviews:\n    default: 2\n"))
    assert cfg.pipeline["reviews"]["default"] == 2


def test_reviews_two_is_accepted_and_three_is_rejected_by_spec_validation(tmp_path: Path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "name: demo\ncreated: 2026-07-14T00:00:00Z\nstatus: specifying\n"
        "current_phase: spec\nnext_action: run\nreviews: 2\n",
        encoding="utf-8",
    )
    assert validate_spec_yaml(spec, None, False) == []
    spec.write_text(
        "name: demo\ncreated: 2026-07-14T00:00:00Z\nstatus: specifying\n"
        "current_phase: spec\nnext_action: run\nreviews: 3\n",
        encoding="utf-8",
    )
    assert validate_spec_yaml(spec, None, False) == [
        "reviews: 3 is not supported; use 0, 1, or 2"
    ]


def test_phase_completion_rejects_three_reviewers_without_downgrading(tmp_path: Path):
    specs = tmp_path / ".builder" / "specs"
    spec_dir = specs / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text("reviews: 3\n", encoding="utf-8")
    (spec_dir / "phase-log.yaml").write_text(
        "phases:\n  - phase: spec\n    completed: 2026-07-14T00:00:00Z\n    outcome: COMPLETE\n",
        encoding="utf-8",
    )

    result = pr.validate_phase_completion(specs, "demo", "spec")

    assert not result.passed
    assert result.reason == "reviews: 3 is not supported; use 0, 1, or 2"
