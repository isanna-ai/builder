from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("planning_contract_characterization", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = planning
spec.loader.exec_module(planning)


def _repo(tmp_path: Path, name: str = "appco") -> Path:
    repo = tmp_path / name
    (repo / ".builder" / "specs").mkdir(parents=True)
    (repo / ".builder" / "releases").mkdir(parents=True)
    return repo


def _spec(repo: Path, spec_id: str, *, status: str = "implementing") -> Path:
    path = repo / ".builder" / "specs" / spec_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "spec.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    return path


def _release(repo: Path, release_id: str, specs: list[str | dict], *, status: str = "active") -> Path:
    lines = [f"release: {release_id}", "product: appco", f"title: {release_id}", f"status: {status}", "specs:"]
    for item in specs:
        if isinstance(item, dict):
            lines.append(f"  - spec: {item['spec']}")
            lines.append(f"    weight: {item['weight']}")
        else:
            lines.append(f"  - spec: {item}")
    path = repo / ".builder" / "releases" / f"{release_id}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _stub_scan(repo: Path, rows: dict[str, str]) -> None:
    planning._scan_cache[str(repo.resolve())] = {"specs": [{"spec": spec_id, "verification": verdict} for spec_id, verdict in rows.items()]}


def test_parse_spec_ref_characterization_matrix():
    cases = {
        "bare": ("ledger", None),
        "qualified": ("sharedlib/node-entrypoint", None),
        "mapping_weight": ({"spec": "sharedlib/node-entrypoint", "weight": 2}, None),
        "too_many": ("a/b/c", "too many segments"),
        "backslash": ("a\\b", "backslash"),
        "traversal": ("../etc/passwd", "too many segments"),
        "empty": ("", "non-empty string"),
    }

    ref, err = planning.parse_spec_ref(cases["bare"][0])
    assert err is None and ref.canonical == "ledger" and ref.weight == 1

    ref, err = planning.parse_spec_ref(cases["qualified"][0])
    assert err is None and ref.canonical == "sharedlib/node-entrypoint"

    ref, err = planning.parse_spec_ref(cases["mapping_weight"][0])
    assert err is None and ref.weight == 2

    for key in ("too_many", "backslash", "traversal", "empty"):
        ref, err = planning.parse_spec_ref(cases[key][0])
        assert ref is None and cases[key][1] in err


def test_registry_and_legacy_product_release_parsers_characterize_current_findings(tmp_path):
    home = _repo(tmp_path, "appco")
    other = _repo(tmp_path, "sharedlib")
    (home / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: appco\n  - alias: sharedlib\n",
        encoding="utf-8",
    )
    (other / ".builder" / "product.yaml").write_text(
        "product: sharedlib\nrepos:\n  - alias: sharedlib\n",
        encoding="utf-8",
    )
    release_path = _release(home, "roadmap", ["appco/a", {"spec": "sharedlib/b", "weight": 2}], status="archived")
    registry = planning.Registry(tmp_path, home)
    release = planning.parse_release(release_path, home)
    product = planning.parse_product(home / ".builder" / "product.yaml")

    assert product.product == "appco"
    assert product.repo_aliases == ["appco", "sharedlib"]
    assert any("claimed by two products" in finding for finding in registry.findings)
    assert release.status == "archived"
    assert [member.weight for member in release.specs] == [1, 2]


def test_completeness_and_lint_characterize_current_member_statuses_and_findings(tmp_path):
    planning._scan_cache.clear()
    repo = _repo(tmp_path)
    _spec(repo, "done", status="verified")
    _spec(repo, "planned", status="planned")
    _spec(repo, "claimed", status="verified")
    rel = planning.parse_release(_release(repo, "roadmap", ["done", "planned", "claimed", "ghost"], status="shipped"), repo)
    _stub_scan(repo, {"done": "host-verified", "claimed": "self-reported"})
    registry = planning.Registry(tmp_path, repo)

    comp = planning.completeness(rel, registry)
    findings = planning.lint_release(rel, registry)

    assert [(member.ref.canonical, member.verification, member.resolved) for member in comp.members] == [
        ("done", "host-verified", True),
        ("planned", "planned", True),
        ("claimed", "self-reported", True),
        ("ghost", "unknown", False),
    ]
    assert (comp.fraction, comp.percent, comp.dangling, comp.planned) == ("1/4", 25, 1, 1)
    assert findings == [f"roadmap: dangling ref 'ghost' (no spec dir at {repo / '.builder' / 'specs' / 'ghost'})"]


def test_cross_repo_cycle_characterization_matches_current_canonical_nodes(tmp_path):
    planning._scan_cache.clear()
    appco = _repo(tmp_path, "appco")
    sharedlib = _repo(tmp_path, "sharedlib")
    (appco / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: appco\n  - alias: sharedlib\n",
        encoding="utf-8",
    )
    _spec(appco, "a").joinpath("dependencies.yaml").write_text(
        "dependencies:\n  - spec: sharedlib/b\n    kind: required\n",
        encoding="utf-8",
    )
    _spec(sharedlib, "b").joinpath("dependencies.yaml").write_text(
        "dependencies:\n  - spec: appco/a\n    kind: required\n",
        encoding="utf-8",
    )
    _release(appco, "roadmap", ["appco/a", "sharedlib/b"], status="shipped")
    registry = planning.Registry(tmp_path, appco)

    cycles = planning.cross_repo_cycles(planning.load_releases(appco), registry)

    assert cycles == [["appco/a", "sharedlib/b", "appco/a"]]
