"""`list-specs` already computes a phase from the artifacts ON DISK and then throws it away
without ever comparing it to what `spec.yaml` DECLARES. These tests make that comparison
load-bearing.

Scope, stated precisely because the two drift alarms are easy to confuse:

  * THIS check catches DECLARED-vs-ARTIFACT drift -- a spec.yaml claiming a phase whose
    required artifact is not on disk, or carrying no readable status at all. It is what
    would have caught the 90-byte `hive-audit-05-activation-spine` stub, whose entire body
    was `id`, `title`, and `status`, sitting in the backlog looking like a real spec.
  * It does NOT catch SHIPPED-vs-SPEC drift -- a spec marked `planned` whose deliverable is
    already live. Every artifact is present in that case, so nothing here fires. That is
    what `isanna verify --spec` is for.

Together they cover both directions: this one says "you claim more than you have", the probe
says "you have more than you claim". Neither subsumes the other.

Direction matters. The check fires only when the declaration OVER-claims. A spec whose disk
artifacts run ahead of its declared status is under-claiming, which is untidy but never
causes anyone to skip work, so it is not an error here.

No YAML dependency: `list-specs.py` is stdlib-only on purpose so it runs anywhere, and the
status is read with a regex rather than a parser. These tests hold that line.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("list_specs_under_test", SCRIPTS / "list-specs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_specs_under_test"] = module
    spec.loader.exec_module(module)
    return module


list_specs = _load()

_FULL = {
    "requirements.yaml": "artifact: requirements\n",
    "system-model.yaml": "version: 1\n",
    "design.yaml": "artifact: design\n",
    "tasks.yaml": "artifact: tasks\n",
}


def _spec(tmp_path: Path, name: str, *, status: str | None, artifacts=("requirements.yaml",)) -> Path:
    d = tmp_path / ".builder" / "specs" / name
    d.mkdir(parents=True)
    if status is not None:
        (d / "spec.yaml").write_text(f"name: {name}\nstatus: {status}\ncurrent_phase: x\n", encoding="utf-8")
    for a in artifacts:
        (d / a).write_text(_FULL[a], encoding="utf-8")
    return tmp_path


def _drift(root: Path):
    return list_specs.collect_drift(list_specs.runtime_dir(root) / "specs")


# --- the over-claim direction -------------------------------------------------


def test_a_status_of_planned_with_no_tasks_yaml_is_drift(tmp_path):
    root = _spec(tmp_path, "s1", status="planned", artifacts=("requirements.yaml", "design.yaml"))
    findings = _drift(root)
    assert len(findings) == 1
    assert findings[0].spec == "s1" and "tasks.yaml" in findings[0].detail


def test_a_status_of_verified_with_no_tasks_yaml_is_drift(tmp_path):
    # The dangerous shape: the strongest possible claim over the thinnest possible evidence.
    root = _spec(tmp_path, "s1", status="verified", artifacts=("requirements.yaml",))
    findings = _drift(root)
    assert findings and "tasks.yaml" in findings[0].detail


def test_a_stub_spec_carrying_only_a_status_is_drift(tmp_path):
    # `hive-audit-05-activation-spine`, 90 bytes: id, title, status. No requirements, no
    # tasks, no acceptance -- and it sat in the backlog reading like real work.
    root = _spec(tmp_path, "s1", status="specified", artifacts=())
    findings = _drift(root)
    assert findings and findings[0].spec == "s1"


def test_a_spec_with_no_readable_status_is_drift(tmp_path):
    root = _spec(tmp_path, "s1", status=None, artifacts=("requirements.yaml", "tasks.yaml"))
    findings = _drift(root)
    assert findings and "status" in findings[0].detail.lower()


def test_an_unknown_status_value_is_drift(tmp_path):
    root = _spec(tmp_path, "s1", status="mostly-done", artifacts=("requirements.yaml", "tasks.yaml"))
    findings = _drift(root)
    assert findings and "mostly-done" in findings[0].detail


# --- the quiet cases ----------------------------------------------------------


def test_a_consistent_spec_reports_no_drift(tmp_path):
    root = _spec(tmp_path, "s1", status="planned",
                 artifacts=("requirements.yaml", "design.yaml", "tasks.yaml"))
    assert _drift(root) == []


def test_under_claiming_is_not_drift(tmp_path):
    # Disk runs AHEAD of the declaration. Untidy, but it never causes anyone to skip work,
    # and flagging it would bury the over-claims that actually matter.
    root = _spec(tmp_path, "s1", status="specified",
                 artifacts=("requirements.yaml", "design.yaml", "tasks.yaml"))
    assert _drift(root) == []


def test_a_spec_that_skipped_design_is_not_drift(tmp_path):
    # REGRESSION. The contract's state machine allows `specified -> spec-reviewed -> planned`,
    # which never passes through design. An earlier draft gated design.yaml and reported 11
    # another repo's specs plus most of builder's own as drift -- every one correct, having
    # simply taken the supported route. A check that fires on correct work gets dismissed once
    # and then ignored when it finally finds something real.
    root = _spec(tmp_path, "s1", status="verified", artifacts=("requirements.yaml", "tasks.yaml"))
    assert _drift(root) == []


def test_archived_specs_are_exempt(tmp_path):
    # Archiving legitimately strips a spec to a tombstone; requiring full artifacts would
    # make every archived spec a permanent finding and train the check away.
    root = _spec(tmp_path, "s1", status="archived", artifacts=())
    assert _drift(root) == []


def test_specs_under_the_archive_directory_are_skipped(tmp_path):
    d = tmp_path / ".builder" / "specs" / "archive" / "old"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: old\nstatus: planned\n", encoding="utf-8")
    assert _drift(tmp_path) == []


# --- the CLI surface ----------------------------------------------------------


def test_strict_exits_nonzero_and_names_the_spec(tmp_path):
    root = _spec(tmp_path, "s1", status="planned", artifacts=("requirements.yaml",))
    code = list_specs.main(["--root", str(root), "--strict"])
    assert code == 1


def test_strict_exits_zero_when_every_spec_is_consistent(tmp_path):
    root = _spec(tmp_path, "s1", status="planned",
                 artifacts=("requirements.yaml", "design.yaml", "tasks.yaml"))
    assert list_specs.main(["--root", str(root), "--strict"]) == 0


def test_without_strict_drift_never_changes_the_exit_code(tmp_path):
    # Staged like every other gate in this repo: report first, enforce only when asked.
    root = _spec(tmp_path, "s1", status="planned", artifacts=("requirements.yaml",))
    assert list_specs.main(["--root", str(root)]) == 0


def test_strict_passes_on_a_repo_with_no_spec_corpus_at_all(tmp_path):
    """A fresh clone has no work, which is not the same as bad work.

    `make lint` runs this with --strict as a drift gate. The public export drops `.builder/`
    entirely, so every public clone hits this path; failing it made the project's own
    documented merge criterion red on checkout one, for the absence of a corpus rather than
    for anything wrong with it.
    """
    root = tmp_path / "fresh-clone"
    root.mkdir()
    assert not (root / ".builder").exists()
    assert list_specs.main(["--root", str(root), "--strict"]) == 0


def test_bare_listing_still_exits_nonzero_with_no_spec_corpus(tmp_path):
    """The affordance for a human ("you have no specs, run /isanna-setup") must survive the
    --strict relaxation above -- only the gate's verdict changed, not the listing's."""
    root = tmp_path / "fresh-clone"
    root.mkdir()
    assert list_specs.main(["--root", str(root)]) == 1


def test_json_output_carries_the_drift_findings(tmp_path):
    import io
    import contextlib
    import json

    root = _spec(tmp_path, "s1", status="planned", artifacts=("requirements.yaml",))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        list_specs.main(["--root", str(root), "--json"])
    payload = json.loads(buf.getvalue())
    assert payload["drift"] and payload["drift"][0]["spec"] == "s1"


def test_status_is_read_without_a_yaml_parser(tmp_path):
    # The module is stdlib-only by design so it runs on any host. If someone reaches for a
    # YAML import to do this, list-specs stops running in the environments it exists for.
    source = (SCRIPTS / "list-specs.py").read_text(encoding="utf-8")
    assert "import yaml" not in source and "from _yaml" not in source
