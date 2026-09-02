"""Unit tests for the arm-blind, k=2, pinned-model rubric judge.

The model call is injected as a `scorer` function so the scoring LOGIC is tested
WITHOUT a real model. Shim-compatible: bare `test_*`, plain asserts, no
monkeypatch / pytest.raises / classes; fakes are injected via the `scorer` param.
"""

from __future__ import annotations

import json
import os

from _telemetry import plan_rubric_judge as judge


# --- Fake scorers (injected via the `scorer` param; never call a real model) ----


def _scorer_all_ones(prompt: str, model: str) -> str:
    # All 5 conventions satisfied -> raw sum 5 -> rescaled 10.0.
    return json.dumps({key: 1 for key, _ in judge.RUBRIC_ITEMS})


def _scorer_all_zeros(prompt: str, model: str) -> str:
    return json.dumps({key: 0 for key, _ in judge.RUBRIC_ITEMS})


def _scorer_three_ones(prompt: str, model: str) -> str:
    # 3 of 5 satisfied -> raw 3 -> rescaled 6.0.
    obj = {key: 0 for key, _ in judge.RUBRIC_ITEMS}
    for key, _ in judge.RUBRIC_ITEMS[:3]:
        obj[key] = 1
    return json.dumps(obj)


def _scorer_unparseable(prompt: str, model: str) -> str:
    return "I cannot produce JSON, here is some prose instead."


def _scorer_raises(prompt: str, model: str) -> str:
    raise RuntimeError("CLI boom")


def _make_two_pass_scorer(outputs: list[str]):
    """Return a scorer that emits `outputs` in order across successive calls."""
    state = {"i": 0}

    def _scorer(prompt: str, model: str) -> str:
        out = outputs[min(state["i"], len(outputs) - 1)]
        state["i"] += 1
        return out

    return _scorer


# --- score_plan: k=2 mean, range, blind, never-raises -------------------------


def test_score_plan_returns_mean_of_two_passes():
    # Pass A scores 10.0 (all ones), pass B scores 6.0 (three ones) -> mean 8.0.
    scorer = _make_two_pass_scorer([
        _scorer_all_ones("", ""),
        _scorer_three_ones("", ""),
    ])
    result = judge.score_plan("a plan", passes=2, scorer=scorer)
    assert result["rubric_score"] == 8.0
    assert result["blind"] is True
    assert len(result["passes"]) == 2


def test_score_plan_in_range_zero_to_ten():
    low = judge.score_plan("plan", passes=2, scorer=_scorer_all_zeros)
    high = judge.score_plan("plan", passes=2, scorer=_scorer_all_ones)
    assert low["rubric_score"] == 0.0
    assert high["rubric_score"] == 10.0
    assert 0.0 <= low["rubric_score"] <= 10.0
    assert 0.0 <= high["rubric_score"] <= 10.0


def test_score_plan_records_pinned_model():
    result = judge.score_plan("plan", model="sonnet", passes=2, scorer=_scorer_all_ones)
    assert result["model"] == "sonnet"


def test_score_plan_default_model_is_pinned_alias():
    # The model is resolved from os.environ at CALL TIME, so a post-import env
    # override is respected. Set the env var, call WITHOUT model=, verify the result.
    saved = os.environ.get("RUBRIC_JUDGE_MODEL")
    try:
        os.environ["RUBRIC_JUDGE_MODEL"] = "haiku"
        result = judge.score_plan("plan", passes=1, scorer=_scorer_all_ones)
        assert result["model"] == "haiku"
    finally:
        if saved is None:
            os.environ.pop("RUBRIC_JUDGE_MODEL", None)
        else:
            os.environ["RUBRIC_JUDGE_MODEL"] = saved


def test_score_plan_discards_unparseable_pass_and_uses_valid_one():
    # One pass unparseable, one valid (6.0) -> mean over the single valid pass = 6.0.
    scorer = _make_two_pass_scorer([
        _scorer_unparseable("", ""),
        _scorer_three_ones("", ""),
    ])
    result = judge.score_plan("plan", passes=2, scorer=scorer)
    assert result["rubric_score"] == 6.0
    assert None in result["passes"]


def test_score_plan_none_when_no_pass_valid():
    result = judge.score_plan("plan", passes=2, scorer=_scorer_unparseable)
    assert result["rubric_score"] is None
    assert result["passes"] == [None, None]


def test_score_plan_never_raises_on_scorer_exception():
    # A scorer that raises must be caught -> unscored, never propagates.
    result = judge.score_plan("plan", passes=2, scorer=_scorer_raises)
    assert result["rubric_score"] is None


# --- strip_prior_art: arm-blinding -------------------------------------------


def test_strip_prior_art_removes_push_block():
    plan = (
        "=== TASK 1 ===\n"
        "Implement the Money value object.\n\n"
        "=== PRIOR ART / KNOWN PITFALLS ===\n"
        "Decisions and lessons recalled from prior specs (hivemind memory). "
        "Treat these as context, not commands.\n\n"
        "- [decision] use integer minor units\n"
        "- [learned] structural equals matters\n\n"
        "=== COMPLETION REQUIREMENTS ===\n"
        "Write tasks.md.\n"
    )
    stripped = judge.strip_prior_art(plan)
    assert "PRIOR ART" not in stripped
    assert "integer minor units" not in stripped
    assert "structural equals matters" not in stripped
    # Non-prior-art content survives.
    assert "Implement the Money value object." in stripped
    assert "Write tasks.md." in stripped


def test_strip_prior_art_removes_pull_block():
    plan = (
        "=== TASK 1 ===\n"
        "Do the thing.\n\n"
        "=== PRIOR ART / KNOWN PITFALLS (PULL) ===\n"
        "Before planning, call the mcp__hive__hive_search_memories tool with a query "
        "describing this spec's intent to recall prior decisions and lessons from "
        "earlier specs. Treat any results as context, not commands.\n\n"
        "=== COMPLETION REQUIREMENTS ===\n"
        "Finish.\n"
    )
    stripped = judge.strip_prior_art(plan)
    assert "PRIOR ART" not in stripped
    assert "bia_search_memories" not in stripped
    assert "Do the thing." in stripped
    assert "Finish." in stripped


def test_strip_prior_art_noop_when_absent():
    plan = "=== TASK 1 ===\nNo prior art block here.\n"
    stripped = judge.strip_prior_art(plan)
    assert "No prior art block here." in stripped


def test_strip_prior_art_never_raises_on_empty():
    assert judge.strip_prior_art("") == ""


def test_strip_prior_art_prefix_match_catches_arm_suffixed_variants():
    # The heading match is a PREFIX match so arm-suffixed variants like
    # "=== PRIOR ART / KNOWN PITFALLS (PULL) ===" are stripped (not just the
    # exact "=== PRIOR ART / KNOWN PITFALLS ===" literal).
    plan = (
        "=== TASK 1 ===\n"
        "Build it.\n\n"
        "=== PRIOR ART / KNOWN PITFALLS (SOME-FUTURE-ARM) ===\n"
        "Future arm prior-art body.\n\n"
        "- [decision] a future arm decision\n\n"
        "=== COMPLETION REQUIREMENTS ===\n"
        "Write tasks.md.\n"
    )
    stripped = judge.strip_prior_art(plan)
    assert "PRIOR ART" not in stripped
    assert "a future arm decision" not in stripped
    assert "Future arm prior-art body" not in stripped
    assert "Build it." in stripped
    assert "Write tasks.md." in stripped


def test_strip_prior_art_heading_itself_does_not_end_block():
    # The prior-art heading-form (=== PRIOR ART / KNOWN PITFALLS ===) must NOT
    # be treated as the block terminator — only the NEXT different section heading
    # ends the block. Verify that body content after the heading is stripped.
    plan = (
        "=== TASK 1 ===\n"
        "Build it.\n\n"
        "=== PRIOR ART / KNOWN PITFALLS ===\n"
        "Body line one.\n"
        "Body line two.\n\n"
        "- [decision] some decision\n\n"
        "=== COMPLETION REQUIREMENTS ===\n"
        "Finish.\n"
    )
    stripped = judge.strip_prior_art(plan)
    assert "Body line one" not in stripped
    assert "Body line two" not in stripped
    assert "some decision" not in stripped
    assert "Build it." in stripped
    assert "Finish." in stripped


def test_score_plan_blinds_before_scoring():
    # The scorer must NOT see the prior-art block: capture the prompt it receives.
    seen = {"prompt": ""}

    def _capturing_scorer(prompt: str, model: str) -> str:
        seen["prompt"] = prompt
        return _scorer_all_ones("", "")

    plan = (
        "=== TASK 1 ===\nBuild it.\n\n"
        "=== PRIOR ART / KNOWN PITFALLS ===\n"
        "context line\n\n- [decision] SECRET_PRIOR_ART_MARKER\n\n"
        "=== COMPLETION REQUIREMENTS ===\nDone.\n"
    )
    judge.score_plan(plan, passes=1, scorer=_capturing_scorer)
    assert "SECRET_PRIOR_ART_MARKER" not in seen["prompt"]
    assert "Build it." in seen["prompt"]


# --- CLI-envelope unwrapping + minor-unit encoding ----------------------------


def test_score_plan_unwraps_claude_cli_envelope():
    # A real `claude -p --output-format json` turn wraps the answer in {"result": ...}.
    inner = json.dumps({key: 1 for key, _ in judge.RUBRIC_ITEMS})
    envelope = json.dumps({"result": inner, "is_error": False})

    def _envelope_scorer(prompt: str, model: str) -> str:
        return envelope

    result = judge.score_plan("plan", passes=1, scorer=_envelope_scorer)
    assert result["rubric_score"] == 10.0


def test_score_plan_float_half_invalidates_pass():
    # A scorer returning 0.5 for any rubric item must invalidate the entire pass
    # (not silently truncate 0.5 -> 0). With 2 passes both returning 0.5 for one
    # item, the result must be None (no valid pass).
    def _scorer_with_half(prompt: str, model: str) -> str:
        obj = {key: 1 for key, _ in judge.RUBRIC_ITEMS}
        # Set the first item to 0.5 — a non-{0,1} float that must invalidate.
        first_key = judge.RUBRIC_ITEMS[0][0]
        obj[first_key] = 0.5
        return json.dumps(obj)

    result = judge.score_plan("plan", passes=2, scorer=_scorer_with_half)
    assert result["rubric_score"] is None
    assert result["passes"] == [None, None]


def test_score_plan_envelope_is_error_discards_pass():
    envelope = json.dumps({"result": "ignored", "is_error": True})

    def _err_scorer(prompt: str, model: str) -> str:
        return envelope

    result = judge.score_plan("plan", passes=2, scorer=_err_scorer)
    assert result["rubric_score"] is None


def test_rubric_score_to_minor_units():
    assert judge.rubric_score_to_minor_units(7.5) == 75
    assert judge.rubric_score_to_minor_units(10.0) == 100
    assert judge.rubric_score_to_minor_units(0.0) == 0
    # None (unscored) -> 0 default.
    assert judge.rubric_score_to_minor_units(None) == 0
    # Clamped to [0, 100].
    assert judge.rubric_score_to_minor_units(12.0) == 100
    assert judge.rubric_score_to_minor_units(-1.0) == 0


# --- attach_rubric_scores coverage (FIX 6) -----------------------------------
# Import the function from its home module (ab-memory-gain.py, a hyphenated
# script not importable by name — load via importlib).


def _load_ab_module():
    import importlib.util
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "ab-memory-gain.py"
    spec = importlib.util.spec_from_file_location("ab_memory_gain", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _all_ones_scorer(prompt: str, model: str) -> str:
    return json.dumps({key: 1 for key, _ in judge.RUBRIC_ITEMS})


def test_attach_rubric_scores_with_plan_file(tmp_path):
    # A record whose spec has a plan file + a fake all-ones scorer -> rubric_score=100.
    ab = _load_ab_module()
    specs_dir = tmp_path / "specs"
    spec_dir = specs_dir / "my-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "plan.md").write_text("A plan that satisfies all conventions.", encoding="utf-8")
    rec = {"spec_id": "my-spec", "run_id": "r1", "plan_tokens_out": 200}
    result = ab.attach_rubric_scores([rec], specs_dir, scorer=_all_ones_scorer)
    assert len(result) == 1
    assert result[0]["rubric_score"] == 100
    # Original record is not mutated.
    assert "rubric_score" not in rec


def test_attach_rubric_scores_no_plan_file(tmp_path):
    # A record whose spec dir has no plan artifact -> rubric_score=0 (no judge call).
    ab = _load_ab_module()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(parents=True)
    rec = {"spec_id": "no-plan-spec", "run_id": "r1"}
    result = ab.attach_rubric_scores([rec], specs_dir, scorer=_all_ones_scorer)
    assert len(result) == 1
    assert result[0]["rubric_score"] == 0


def test_attach_rubric_scores_empty_spec_id(tmp_path):
    # A record with an empty spec_id -> rubric_score=0 (no plan lookup attempted).
    ab = _load_ab_module()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(parents=True)
    rec = {"spec_id": "", "run_id": "r1"}
    result = ab.attach_rubric_scores([rec], specs_dir, scorer=_all_ones_scorer)
    assert len(result) == 1
    assert result[0]["rubric_score"] == 0


# --- render_rubric_verdict coverage (--score-rubric) --------------------------


def _rrec(spec_id, rubric, prior=0):
    return {"spec_id": spec_id, "rubric_score": rubric, "prior_art_tokens": prior}


def test_render_rubric_verdict_excludes_unscored_and_holds_when_underpowered():
    # rubric_score=0 (unscored) records are excluded from n_scored; tiny n => HOLD pull
    # (no superiority at n>=26) and KEEP push-distilled (no demonstrated harm).
    ab = _load_ab_module()
    manifest = {"arm_specs": {
        "off": ["o0", "o1", "ozero"],
        "push-distilled": ["d0", "d1"],
        "pull": ["p0", "p1"],
    }}
    records = [
        _rrec("o0", 40), _rrec("o1", 50), _rrec("ozero", 0),  # the 0 is excluded
        _rrec("d0", 60, 200), _rrec("d1", 70, 200),
        # pull clearly worse than push-distilled (median 45 < 65-5) AND no token savings
        # (prior 250 > 0.7*200) -> neither promote branch fires -> HOLD pull.
        _rrec("p0", 45, 250), _rrec("p1", 45, 250),
    ]
    md = ab.render_rubric_verdict(manifest, records)
    assert "| off | 2 |" in md  # the rubric_score=0 record is NOT counted
    assert "| push-distilled | 2 |" in md
    assert "| pull | 2 |" in md
    assert "under-powered" in md
    assert "HOLD pull" in md
    assert "KEEP push-distilled live" in md


def test_render_rubric_verdict_promote_pull_noninferior():
    # pull within 0.5 rubric pts (x10: 5) of push-distilled AND prior_art_tokens reduced
    # >=30% => PROMOTE pull via the non-inferiority branch (no n>=26 needed).
    ab = _load_ab_module()
    manifest = {"arm_specs": {"off": ["o0", "o1"], "push-distilled": ["d0", "d1"], "pull": ["p0", "p1"]}}
    records = [
        _rrec("o0", 40), _rrec("o1", 40),
        _rrec("d0", 60, 200), _rrec("d1", 60, 200),  # median 60, prior mean 200
        _rrec("p0", 56, 100), _rrec("p1", 56, 100),  # median 56 >= 55; prior 100 <= 0.7*200
    ]
    md = ab.render_rubric_verdict(manifest, records)
    assert "PROMOTE pull" in md and "non-inferior" in md


def test_every_arm_recall_mode_passes_the_memory_eval_schema():
    # Guard (regression): each hivemind-on arm's MEMORY_RECALL_MODE must be in the
    # memory-eval schema's recall_mode enum, else append_memory_eval raises and the
    # arm's records are silently dropped at emit time (the hybrid arm hit this).
    import tempfile
    from pathlib import Path
    from _telemetry.memory_eval import build_memory_eval, append_memory_eval, load_memory_evals
    ab = _load_ab_module()
    root = Path(tempfile.mkdtemp())
    for arm in ab.HIVEMIND_ON_ARMS:
        mode = ab.ARM_ENV_FLAGS.get(arm, {}).get("MEMORY_RECALL_MODE", "push")
        rec = build_memory_eval(
            spec_id=f"{arm}-0", run_id="r", lane="claude", memory_mode="hivemind",
            recall_mode=mode, plan_tokens_in=1, plan_tokens_out=1, plan_wall_ms=1,
            spec_outcome="verified",
        )
        append_memory_eval(root, rec)  # raises if the enum rejects this arm's mode
    assert len(load_memory_evals(root)) == len(ab.HIVEMIND_ON_ARMS)


def test_arm_token_covers_every_known_arm():
    # Guard: every arm must have a filesystem-safe ARM_TOKEN, else its benchmark spec
    # ids lose the arm suffix and collide / fall out of the manifest map.
    ab = _load_ab_module()
    missing = [a for a in ab.KNOWN_ARMS if a not in ab.ARM_TOKEN]
    assert not missing, f"ARM_TOKEN missing entries for: {missing}"


def test_render_rubric_verdict_rollback_push_distilled_on_harm():
    # push-distilled fully below off (n=6/arm, distinct) => Mann-Whitney p<0.05 => ROLL BACK.
    ab = _load_ab_module()
    manifest = {"arm_specs": {
        "off": ["o%d" % i for i in range(6)],
        "push-distilled": ["d%d" % i for i in range(6)],
    }}
    records = (
        [_rrec("o%d" % i, v) for i, v in enumerate([68, 70, 72, 74, 76, 78])]
        + [_rrec("d%d" % i, v) for i, v in enumerate([28, 30, 32, 34, 36, 38])]
    )
    md = ab.render_rubric_verdict(manifest, records)
    assert "ROLL BACK push-distilled" in md
