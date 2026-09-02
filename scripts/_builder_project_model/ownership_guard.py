from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .draining import FederatedDrainer
from .home import load_optional_home


@dataclass(frozen=True)
class OwnershipGuardResult:
    repo_root: Path
    owned: bool
    repo_id: str | None
    home_root: Path | None
    findings: tuple[str, ...] = ()


def evaluate_repo_ownership(repo_root: Path) -> OwnershipGuardResult:
    repo_root = repo_root.resolve()
    findings: list[str] = []
    try:
        home = load_optional_home(start=repo_root)
    except Exception as exc:  # noqa: BLE001 - entrypoints must degrade to standalone
        findings.append(f"{repo_root}: Builder Home read degraded to standalone: {exc}")
        return OwnershipGuardResult(
            repo_root=repo_root,
            owned=False,
            repo_id=None,
            home_root=None,
            findings=tuple(findings),
        )
    if home is None:
        return OwnershipGuardResult(repo_root=repo_root, owned=False, repo_id=None, home_root=None)

    repo_id = next((candidate_id for candidate_id, root in home.repo_roots_by_id.items() if root == repo_root), None)
    if repo_id is None:
        findings.append(f"{repo_root}: resolved home {home.root} does not register this repo; treating as standalone")
        return OwnershipGuardResult(
            repo_root=repo_root,
            owned=False,
            repo_id=None,
            home_root=home.root,
            findings=tuple(findings),
        )

    owned = repo_id in FederatedDrainer(home, owner_id="phase7-ownership-guard").allowed_repo_ids()
    return OwnershipGuardResult(
        repo_root=repo_root,
        owned=owned,
        repo_id=repo_id,
        home_root=home.root,
        findings=tuple(findings),
    )


def refusal_message(result: OwnershipGuardResult, *, launcher_label: str) -> str:
    repo_label = result.repo_id or str(result.repo_root)
    home_label = str(result.home_root) if result.home_root is not None else "(unknown home)"
    return (
        f"refuse {launcher_label}: repo {repo_label} is owned by Builder Home {home_label}; "
        "launch authority is the central daemon"
    )
