"""Shared safety gates for the opt-in live central runtime."""

from __future__ import annotations

from pathlib import Path

from .home import BuilderHome, load_optional_home


CENTRAL_OWNER_PREFIX = "central-"


def repo_id_for_root(home: BuilderHome, repo_root: Path) -> str | None:
    resolved = Path(repo_root).resolve()
    return next((repo_id for repo_id, root in home.repo_roots_by_id.items() if root == resolved), None)


def live_activation(home: BuilderHome | None, repo_id: str | None = None) -> bool:
    """The single activation predicate used by every live entrypoint.

    A daemon-level check omits ``repo_id`` but still requires a non-empty allow-list.
    A repo-level check additionally requires membership in that allow-list.
    """

    if home is None or not home.policy.governor_enabled or not home.policy.drain_repos:
        return False
    return repo_id is None or repo_id in home.policy.drain_repos


def activated_home_for_repo(*, repo_root: Path, home_path: Path | None = None) -> tuple[BuilderHome, str]:
    home = load_optional_home(start=repo_root, home=home_path)
    repo_id = None if home is None else repo_id_for_root(home, repo_root)
    if not live_activation(home, repo_id):
        raise RuntimeError(
            "live central runtime is inactive: requires Builder Home, "
            "governor.enabled=true, and this repo in governor.drain_repos"
        )
    assert home is not None and repo_id is not None
    return home, repo_id
