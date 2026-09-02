"""Tests for the fail-closed public pre-publish scrub gate."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("scrub_under_test", ROOT / "scripts" / "pre-publish-scan.py")
scrub = importlib.util.module_from_spec(_spec)
sys.modules["scrub_under_test"] = scrub
_spec.loader.exec_module(scrub)


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _track(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)


def test_catches_generic_secret_and_pii_shapes(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "app.py", 'KEY = "ghp_' + "a" * 36 + '"\n')  # publish-ok: scanner fixture
    _track(repo, "note.md", "reach me at someone@example.com\n")  # publish-ok: scanner fixture
    _track(repo, "deploy.yml", "vol: /Users/example/x\n")  # publish-ok: scanner fixture
    names = {finding[2] for finding in scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))}
    assert {"github token", "email address", "user home path"} <= names


def test_telegram_chat_id_is_caught(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "cfg.json", '{"chat_id": -1001234567890}\n')  # publish-ok: scanner fixture
    assert any(f[2] == "telegram chat id" for f in scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES)))


def test_excluded_paths_are_not_scanned(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, ".builder/specs/x/spec.yaml", 'token: "ghp_' + "b" * 36 + '"\n')  # publish-ok: scanner fixture
    _track(repo, "docs/planning/secret.md", "chat -1001234567890\n")  # publish-ok: scanner fixture
    assert scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES)) == []
    assert scrub.scan(repo, scrub.tracked_files(repo))


def test_publish_ok_marker_clears_a_line(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "example.md", 'set OPENAI_API_KEY=sk-' + "x" * 24 + '  # publish-ok: doc placeholder\n')
    assert scrub.main(["--root", str(repo)]) == 0


def test_private_fleet_coupling_and_marker(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "fleet.md", "/workspaces/" + "builder\n/" + "lab/config\n/opt-" + "hermes/bin\nbuilder-daemon-watchdog\n")  # publish-ok: scanner fixture
    names = {finding[2] for finding in scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))}
    assert {"private fleet path", "private lab path", "private hermes path", "private watchdog"} <= names
    _track(repo, "cleared.md", "/workspaces/" + "builder  # publish-ok: fixture\n")
    assert any(f[0] == "fleet.md" for f in scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES)))


def test_unquoted_env_style_secrets_are_caught(tmp_path):
    """A `.env` quotes nothing, and its names are compound. The rule used to require a quoted
    value AND a bare keyword, so a tracked `.env` sailed through completely -- measured on a
    planted repo: 3 of 4 secrets passed. SECURITY.md invites third parties to rely on this gate
    for their own publication, so it has to cover the shape secrets are actually written in."""
    repo = _git_repo(tmp_path)
    # Assembled at RUNTIME. Written as literals the compiler folds them, and the folded constants
    # map to one line -- past the `publish-ok` markers -- so this file would flag ITSELF. The same
    # trap caught the fleet-path fixture above.
    names = ["AWS_" + "SECRET_ACCESS_KEY", "DB_" + "PASSWORD", "API_" + "KEY"]
    values = ["wJalrXUtnFEMIK7MDENGbPxRfiCY", "hunter2hunter2hunter2", "abcdef1234567890abcdef"]
    body = "".join(f"{n}={v}\n" for n, v in zip(names, values))
    _track(repo, "dotenv.txt", body)
    findings = scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))
    hits = [f for f in findings if f[2] == "generic secret assignment"]
    assert len(hits) == 3, f"expected all three unquoted assignments, got {hits}"


def test_an_env_var_name_held_as_a_value_is_not_a_secret(tmp_path):
    """The other half. Source code routinely stores an env var NAME as a string --
    `_TOKEN_FILE_ENV = "ISANNA_CLAUDE_CODE_OAUTH_TOKEN_FILE"` -- and a rule that flags those
    produces noise that gets the gate bypassed. Widening the quoted branch to compound names
    fired on 3 such lines in this repo; the quoted branch therefore keeps its word boundary."""
    repo = _git_repo(tmp_path)
    _track(repo, "names.py", '_TOKEN_FILE_ENV = "ISANNA_CLAUDE_CODE_OAUTH_TOKEN_FILE"\n')
    findings = scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))
    assert [f for f in findings if f[2] == "generic secret assignment"] == []


def test_a_dot_named_fleet_path_is_not_exempt(tmp_path):
    """Regression: the fleet lookahead used to list `\\.` alongside the doc placeholders, which
    exempted EVERY dot-prefixed path under a workspace root -- runtime dirs and dot-named
    worktrees included. A real one reached standards/builder-contract.md, a file the installer
    copies into every user's project, and this gate reported CLEAN. A leading dot is not a
    placeholder; it is the shape most likely to be a private runtime."""
    repo = _git_repo(tmp_path)
    _track(repo, "leak.md", "audited /work" + "spaces/.some-worktree yesterday\n")  # publish-ok: scanner fixture
    findings = scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))
    assert "private fleet path" in {f[2] for f in findings}


def test_documented_workspace_placeholders_stay_exempt(tmp_path):
    """The other half of the contract: the lookahead must still clear the placeholders docs use,
    or every install example becomes a finding and the gate gets ignored."""
    repo = _git_repo(tmp_path)
    # Joined at RUNTIME, deliberately. Written as one literal, the compiler folds it into a
    # single multi-line string, and the exemptions are anchored with `$` -- which does not match
    # before an interior newline. The fixture would then flag THIS test file, which is a fact
    # about literal folding, not about the placeholders.
    root = "/work" + "spaces/"
    _track(repo, "doc.md", "\n".join(root + name for name in ("example", "tmp", "repo", "<repo>/x")) + "\n")
    findings = scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))
    assert [f for f in findings if f[2] == "private fleet path"] == []


def test_private_denylist_is_nonpublished_and_loaded(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "owner.yaml", "owner: octocat\n")
    denylist = repo / "scripts" / "_scrub_private.txt"
    denylist.parent.mkdir(parents=True, exist_ok=True)
    denylist.write_text("octocat\n", encoding="utf-8")
    findings = scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))
    assert any(f[2] == "private denylist" for f in findings)
    assert "scripts/_scrub_private.txt" not in scrub.publishable(repo, scrub.DEFAULT_EXCLUDES)


def test_secret_suffixes_are_always_scanned(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, ".env.prod", 'TOKEN="ghp_' + "a" * 36 + '"\n')  # publish-ok: scanner fixture
    _track(repo, "private.key", "-----BEGIN PRIVATE KEY-----\n")  # publish-ok: scanner fixture
    names = {finding[2] for finding in scrub.scan(repo, scrub.publishable(repo, scrub.DEFAULT_EXCLUDES))}
    assert {"github token", "private key block"} <= names


def test_unreadable_and_unparseable_assets_fail_closed(tmp_path):
    repo = _git_repo(tmp_path)
    path = repo / "broken.svg"
    path.write_bytes(b"<svg>\xff</svg>")
    subprocess.run(["git", "-C", str(repo), "add", "broken.svg"], check=True)
    assert scrub.main(["--root", str(repo)]) == 1
    repo = _git_repo(tmp_path / "syntax")
    _track(repo, "broken.py", "def nope(:\n")
    assert scrub.main(["--root", str(repo)]) == 1


def test_git_error_and_empty_publishable_set_fail_closed(tmp_path):
    assert scrub.main(["--root", str(tmp_path)]) == 2
    repo = _git_repo(tmp_path / "empty")
    _track(repo, "docs/PUBLISH.md", "private\n")
    assert scrub.main(["--root", str(repo)]) == 2


def test_clean_tree_passes(tmp_path):
    repo = _git_repo(tmp_path)
    _track(repo, "readme.md", "A normal project with no secrets.\n")
    assert scrub.main(["--root", str(repo)]) == 0
