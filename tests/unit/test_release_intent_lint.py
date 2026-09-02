from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import planning


def _intent(root: Path, intent_id: str, specs: list[str], *, status: str = "accepted") -> None:
    path = root / ".builder" / "intents" / intent_id / "intent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    reason = "reason: no longer owned\n" if status in {"rejected", "superseded"} else ""
    path.write_text(
        "artifact: intent-object\n"
        f"intent: {intent_id}\n"
        f"title: {intent_id}\n"
        f"status: {status}\n"
        "problem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\n"
        "non_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\n"
        + reason
        + ("specs:\n" + "".join(f"  - {member}\n" for member in specs) if specs else "specs: []\n"),
        encoding="utf-8",
    )


def _release(root: Path, intents: list[str]) -> planning.Release:
    path = root / ".builder" / "releases" / "demo.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "release: demo\nproduct: demo\ntitle: Demo\nstatus: active\nintents:\n"
        + "".join(f"  - {intent}\n" for intent in intents),
        encoding="utf-8",
    )
    return planning.parse_release(path, root)


def test_lint_names_the_owning_intent_and_unresolved_cross_repo_member(tmp_path):
    root = tmp_path / "repo"
    _intent(root, "broken-owner", ["missing-alias/missing-spec"])
    release = _release(root, ["broken-owner"])

    findings = planning.lint_release(release, planning.Registry(tmp_path, root))

    assert len(findings) == 1
    assert "intent broken-owner" in findings[0]
    assert "missing-alias/missing-spec" in findings[0]


def test_lint_keeps_rejected_or_superseded_intents_blocking(tmp_path):
    for status in ("rejected", "superseded"):
        root = tmp_path / status / "repo"
        _intent(root, "retired-owner", [], status=status)
        release = _release(root, ["retired-owner"])

        comp = planning.completeness(release, planning.Registry(root.parent, root))
        findings = planning.lint_release(release, planning.Registry(root.parent, root))

        assert comp.total == 1 and comp.verified == 0 and comp.dangling == 1
        assert any(f"references {status} intent" in finding for finding in findings)


def test_lint_reports_duplicate_spec_ownership_across_release_intents(tmp_path):
    root = tmp_path / "repo"
    spec = root / ".builder" / "specs" / "same" / "spec.yaml"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("status: planned\n", encoding="utf-8")
    _intent(root, "first-owner", ["same"])
    _intent(root, "second-owner", ["same"])
    release = _release(root, ["first-owner", "second-owner"])

    findings = planning.lint_release(release, planning.Registry(tmp_path, root))

    assert any("owned by both intents 'first-owner' and 'second-owner'" in finding for finding in findings)


def test_release_status_fails_loudly_for_unmigrated_live_membership(tmp_path):
    root = tmp_path / "repo"
    release_path = root / ".builder" / "releases" / "legacy-live.yaml"
    release_path.parent.mkdir(parents=True)
    release_path.write_text(
        "release: legacy-live\nproduct: demo\ntitle: Legacy live\nstatus: active\nspecs:\n  - old-member\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        root=str(root), projects_root=str(tmp_path), release_id="legacy-live", verbose=False
    )

    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert planning.cmd_release_status(args) == 1
    assert str(release_path) in stderr.getvalue()
    assert "live releases must not declare specs" in stderr.getvalue()


def test_release_status_names_a_dangling_intent_without_verbose_mode(tmp_path):
    root = tmp_path / "repo"
    _release(root, ["missing-owner"])
    args = SimpleNamespace(
        root=str(root), projects_root=str(tmp_path), release_id="demo", verbose=False
    )

    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert planning.cmd_release_status(args) == 1
    assert "intent missing-owner" in stderr.getvalue()
    assert ".builder/intents/missing-owner/intent.yaml" in stderr.getvalue()
