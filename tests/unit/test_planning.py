"""The planning layer's tests are about the ONE property that makes it worth building: a release's
% done is computed only from host-observed events, so an agent cannot inflate it. Everything else
(the resolver's path safety, cycle detection, ship being human-only) protects that property.

No pytest fixtures beyond tmp_path — this repo runs a minimal pytest shim. The gate-coverage scan is
stubbed by pre-seeding planning._scan_cache, which is the module's own caching seam.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("planning_under_test", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(_spec)
sys.modules["planning_under_test"] = planning
_spec.loader.exec_module(planning)


# ---------------------------------------------------------------- fixture helpers

def _repo(tmp_path: Path, name: str = "appco") -> Path:
    repo = tmp_path / name
    (repo / ".builder" / "specs").mkdir(parents=True)
    (repo / ".builder" / "releases").mkdir(parents=True)
    return repo


def _spec_dir(repo: Path, spec_id: str, status: str = "implementing") -> Path:
    d = repo / ".builder" / "specs" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    return d


def _release(repo: Path, release_id: str, specs: list, status: str = "shipped",
             product: str = "appco") -> Path:
    lines = [f"release: {release_id}", f"product: {product}", f"title: {release_id}",
             f"status: {status}"]
    if status in {"draft", "active"}:
        intents_dir = repo / ".builder" / "intents"
        intents_dir.mkdir(parents=True, exist_ok=True)
        intent_ids: list[str] = []
        for index, s in enumerate(specs, 1):
            spec_ref = s if isinstance(s, str) else s["spec"]
            intent_id = f"{release_id}-intent-{index}"
            intent_ids.append(intent_id)
            intent_dir = intents_dir / intent_id
            intent_dir.mkdir(parents=True, exist_ok=True)
            (intent_dir / "intent.yaml").write_text(
                "\n".join([
                    "artifact: intent-object",
                    f"intent: {intent_id}",
                    f"title: {intent_id}",
                    "status: accepted",
                    "problem: p",
                    "why: w",
                    "success_criteria:",
                    "  - id: sc-1",
                    "    statement: s",
                    "non_goals:",
                    "  - n",
                    "ssot_delta:",
                    "  capabilities:",
                    "    - target: c",
                    "      change: create",
                    "  behaviors:",
                    "    - target: b",
                    "      change: create",
                    "  journeys:",
                    "    - target: j",
                    "      change: create",
                    "specs:",
                    f"  - {spec_ref}",
                    "",
                ]),
                encoding="utf-8",
            )
        lines.append("intents:")
        for intent_id in intent_ids:
            lines.append(f"  - {intent_id}")
    else:
        lines.append("specs:")
        for s in specs:
            lines.append(f"  - spec: {s}" if isinstance(s, str) else
                         f"  - spec: {s['spec']}\n    weight: {s['weight']}")
    path = repo / ".builder" / "releases" / f"{release_id}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _stub_scan(repo: Path, verifications: dict[str, str]) -> None:
    """Pre-seed the gate-coverage scan cache so completeness reads controlled host stamps.

    The row key is `spec` — the shape gate-coverage's real scan_repo emits (see stamp_spec).
    Seeding `spec_id` here silently matched a lookup bug that never matched a real scan."""
    planning._scan_cache[str(repo.resolve())] = {
        "specs": [{"spec": sid, "verification": v} for sid, v in verifications.items()]
    }


def _clear_cache() -> None:
    planning._scan_cache.clear()


def test_default_scan_repo_call_excludes_a_mutated_chain_from_the_numerator(tmp_path):
    """B4 FLIP integration: planning._scan_repo calls gate-coverage's scan_repo with NO
    check_chain kwarg — the flip means it now gets chain verification for free. A
    gate-evidence bundle mutated after write (recorded bundle_sha256 no longer matches its
    own bytes) must exclude that spec from the host-verified numerator, using the REAL
    gate-coverage scan (not the _stub_scan cache seam the other tests use)."""
    _clear_cache()
    import json as _json
    repo = _repo(tmp_path)
    (repo / ".builder" / "dispatch.yaml").write_text(
        _json.dumps({"queue_store": {"path": ".builder/dispatch-queue"}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _spec_dir(repo, "demo", status="verified")
    _release(repo, "rel", ["demo"])

    sys.path.insert(0, str(SCRIPTS)) if str(SCRIPTS) not in sys.path else None
    from _dispatch_runtime import gate_evidence

    spec_dir = repo / ".builder" / "specs" / "demo"
    body = {"spec_id": "demo", "phase": "verify", "gate": "host_verify", "verdict": "pass"}
    bundle_path = gate_evidence.write_bundle(spec_dir / "gate-evidence", body)
    assert bundle_path is not None

    attempts_dir = repo / ".builder" / "dispatch-queue" / "queue" / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempt = {
        "attempt_id": "attempt-1",
        "metadata": {
            "spec_id": "demo", "phase": "verify", "decision": "phase-complete",
            "reason": "outcome: SUCCEEDED", "started_at": "2026-07-13T00:00:00Z",
            "gates": {"host_verify": "pass"},
            "gate_evidence": [{"path": str(bundle_path.relative_to(spec_dir)),
                                "sha256": body["bundle_sha256"]}],
        },
        "created_at": "2026-07-13T00:00:00Z",
    }
    (attempts_dir / "attempt-1.yaml").write_text(_json.dumps(attempt, indent=2) + "\n", encoding="utf-8")

    # Mutate captured bytes post-write, WITHOUT recomputing bundle_sha256 — the stored hash
    # no longer matches the bundle's own bytes (the same class of corruption a blind
    # rename/sed sweep can inflict on a captured command string).
    raw = bundle_path.read_text(encoding="utf-8")
    mutated = raw.replace("host_verify", "host_verify_mutated", 1)
    assert mutated != raw
    bundle_path.write_text(mutated, encoding="utf-8")

    registry = planning.Registry(tmp_path, repo)
    comp = planning.completeness(planning.find_release(repo, "rel"), registry)
    assert comp.verified == 0, "a mutated gate-evidence chain must not count toward the host-verified numerator"
    assert comp.fraction == "0/1"


# ---------------------------------------------------------------- the ref grammar (path safety)

def test_bare_ref_is_home_repo():
    ref, err = planning.parse_spec_ref("idempotent-ledger")
    assert err is None and ref.alias is None and ref.spec_id == "idempotent-ledger"


def test_qualified_ref_is_cross_repo():
    ref, err = planning.parse_spec_ref("sharedlib/node-entrypoint")
    assert err is None and ref.alias == "sharedlib" and ref.spec_id == "node-entrypoint"


def test_ref_rejects_traversal_and_junk():
    # Each of these could become a path escape if turned into a filesystem path by concatenation.
    for bad in ["../etc/passwd", "a/b/c", "sharedlib/../appco", "..", ".", "sharedlib/..",
                "back\\slash", "/abs", "sharedlib/", "/", ""]:
        ref, err = planning.parse_spec_ref(bad)
        assert ref is None and err, f"expected {bad!r} to be rejected"


def test_ref_weight_from_mapping():
    ref, err = planning.parse_spec_ref({"spec": "sharedlib/x", "weight": 3})
    assert err is None and ref.weight == 3
    ref2, _ = planning.parse_spec_ref({"spec": "y", "weight": 0})  # weight<1 falls back to 1
    assert ref2.weight == 1


# ---------------------------------------------------------------- THE anti-gaming property

def test_percent_done_counts_only_host_verified(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    for sid in ("a", "b", "c", "d"):
        _spec_dir(repo, sid)
    _release(repo, "rel", ["a", "b", "c", "d"])
    # The HOST stamped only a and b verified; c is self-reported, d unknown.
    _stub_scan(repo, {"a": "host-verified", "b": "host-verified",
                      "c": "self-reported", "d": "unknown"})
    registry = planning.Registry(tmp_path, repo)
    rel = planning.find_release(repo, "rel")
    comp = planning.completeness(rel, registry)
    assert comp.fraction == "2/4" and comp.percent == 50


def test_agent_setting_status_verified_moves_the_number_by_zero(tmp_path):
    # THE test. An agent writes `status: verified` into every spec.yaml it controls. The number
    # must not move, because the numerator reads the HOST's gate-coverage stamp, never spec.yaml.
    _clear_cache()
    repo = _repo(tmp_path)
    for sid in ("a", "b", "c"):
        _spec_dir(repo, sid, status="verified")  # agent claims all three are done
    _release(repo, "rel", ["a", "b", "c"])
    _stub_scan(repo, {"a": "host-verified", "b": "self-reported", "c": "unknown"})
    registry = planning.Registry(tmp_path, repo)
    comp = planning.completeness(planning.find_release(repo, "rel"), registry)
    assert comp.verified == 1, "only the host-verified spec counts, regardless of self-declared status"
    assert comp.fraction == "1/3"


def test_dangling_member_counts_against_denominator_never_numerator(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "real")
    _release(repo, "rel", ["real", "ghost"])  # ghost has no spec dir
    _stub_scan(repo, {"real": "host-verified"})
    registry = planning.Registry(tmp_path, repo)
    comp = planning.completeness(planning.find_release(repo, "rel"), registry)
    assert comp.total == 2 and comp.verified == 1 and comp.dangling == 1
    assert comp.fraction == "1/2"


def test_planned_member_is_resolved_but_never_verified(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "future", status="planned")
    _release(repo, "rel", ["future"])
    registry = planning.Registry(tmp_path, repo)
    release = planning.find_release(repo, "rel")
    original_verification = planning._spec_verification
    planning._spec_verification = lambda *_: (_ for _ in ()).throw(
        AssertionError("planned specs must not invoke gate coverage"))
    try:
        comp = planning.completeness(release, registry)
    finally:
        planning._spec_verification = original_verification
    assert comp.total == 1 and comp.verified == 0 and comp.planned == 1
    assert comp.dangling == 0 and comp.fraction == "0/1" and comp.percent == 0
    assert comp.members[0].verification == planning.PLANNED
    assert planning.lint_release(release, registry) == []
    assert planning._segments(comp) == (
        "[host-verified 0 · planned 1 · self-reported 0 · unknown 0]")


def test_empty_release_is_zero_not_a_crash(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _release(repo, "rel", [])
    registry = planning.Registry(tmp_path, repo)
    comp = planning.completeness(planning.find_release(repo, "rel"), registry)
    assert comp.total == 0 and comp.verified == 0 and comp.percent == 0


def test_backlog_collision_inventory_does_not_change_release_completeness(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "a", status="planned")
    _spec_dir(repo, "b", status="planned")
    _release(repo, "rel", ["a", "b"], status="active")
    registry = planning.Registry(tmp_path, repo)
    release = planning.find_release(repo, "rel")
    before = planning.completeness(release, registry)

    index, diagnostics = planning.active_backlog_capability_index(repo, registry)
    after = planning.completeness(release, registry)

    assert diagnostics == []
    assert len(index["c"].collision_intent_ids) == 2
    assert (after.verified, after.total, after.percent) == (before.verified, before.total, before.percent)


# ---------------------------------------------------------------- resolver / registry safety

def test_unknown_alias_never_becomes_a_path(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    ref, _ = planning.parse_spec_ref("nonexistent/spec")
    registry = planning.Registry(tmp_path, repo)  # no product.yaml declares 'nonexistent'
    root, err = registry.resolve(ref)
    assert root is None and "unknown_repo_alias" in err


def test_declared_alias_resolves_and_is_contained(tmp_path):
    _clear_cache()
    appco = _repo(tmp_path, "appco")
    sharedlib = _repo(tmp_path, "sharedlib")
    (appco / ".builder" / "product.yaml").write_text(
        "product: appco\ntitle: Appco\nrepos:\n  - alias: appco\n  - alias: sharedlib\n", encoding="utf-8")
    _spec_dir(sharedlib, "node-entrypoint")
    registry = planning.Registry(tmp_path, appco)
    ref, _ = planning.parse_spec_ref("sharedlib/node-entrypoint")
    spec_dir, err = registry.spec_dir(ref)
    assert err is None and spec_dir == (sharedlib / ".builder" / "specs" / "node-entrypoint").resolve()


def test_symlinked_specs_dir_cannot_escape_the_repo(tmp_path):
    # An agent runs as the same OS user and could point `.builder/specs` at somewhere outside
    # the repo. Resolving the specs dir first would accept a member landing in the symlink target;
    # containment must be judged against the REAL repo root, so the ref is refused.
    _clear_cache()
    import os
    outside = tmp_path / "outside"
    (outside / "leak").mkdir(parents=True)
    (outside / "leak" / "spec.yaml").write_text("status: verified\n", encoding="utf-8")
    repo = tmp_path / "appco"
    (repo / ".builder").mkdir(parents=True)
    os.symlink(outside, repo / ".builder" / "specs")  # specs -> /tmp/outside
    registry = planning.Registry(tmp_path, repo)
    ref, _ = planning.parse_spec_ref("leak")
    spec_dir, err = registry.spec_dir(ref)
    assert spec_dir is None and err and "outside" in err.lower()


def test_symlinked_spec_dir_member_cannot_escape(tmp_path):
    # The spec-id itself being a symlink out must also be refused.
    _clear_cache()
    import os
    secret = tmp_path / "secret"; secret.mkdir()
    (secret / "spec.yaml").write_text("status: verified\n", encoding="utf-8")
    repo = _repo(tmp_path, "appco")
    os.symlink(secret, repo / ".builder" / "specs" / "evil")
    registry = planning.Registry(tmp_path, repo)
    ref, _ = planning.parse_spec_ref("evil")
    spec_dir, err = registry.spec_dir(ref)
    assert spec_dir is None and err


def test_symlinked_yaml_inside_a_valid_spec_dir_is_not_followed(tmp_path):
    # A committed `dependencies.yaml -> /tmp/outside.yaml` inside an otherwise-valid spec dir must
    # not be read: containment guards the dir, but the files read inside it are fixed paths, and a
    # symlinked one escapes with no race. _safe_load refuses any symlinked artifact.
    _clear_cache()
    import os
    outside = tmp_path / "outside.yaml"
    outside.write_text("dependencies:\n  - spec: injected-node\n", encoding="utf-8")
    repo = _repo(tmp_path, "appco")
    spec = _spec_dir(repo, "foo")
    os.symlink(outside, spec / "dependencies.yaml")  # symlink the file, dir is real
    assert planning._safe_load(spec / "dependencies.yaml") is None, "a symlinked YAML must not be read"
    # and the injected edge never enters the graph
    _release(repo, "rel", ["foo"])
    registry = planning.Registry(tmp_path, repo)
    cycles = planning.cross_repo_cycles(planning.load_releases(repo), registry)
    assert cycles == []


def test_two_product_files_for_one_product_is_a_finding(tmp_path):
    _clear_cache()
    a = _repo(tmp_path, "a"); b = _repo(tmp_path, "b")
    (a / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: a\n", encoding="utf-8")
    (b / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: b\n", encoding="utf-8")
    registry = planning.Registry(tmp_path, a)
    assert any("two home repos" in f for f in registry.findings)


def test_two_products_claiming_one_alias_is_a_finding(tmp_path):
    _clear_cache()
    a = _repo(tmp_path, "a")
    b = _repo(tmp_path, "b")
    (a / ".builder" / "product.yaml").write_text(
        "product: pa\nrepos:\n  - alias: shared\n", encoding="utf-8")
    (b / ".builder" / "product.yaml").write_text(
        "product: pb\nrepos:\n  - alias: shared\n", encoding="utf-8")
    registry = planning.Registry(tmp_path, a)
    assert any("claimed by two products" in f for f in registry.findings)


# ---------------------------------------------------------------- lint & cycles

def test_lint_flags_dangling_ref(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "real")
    rel = planning.parse_release(_release(repo, "rel", ["real", "ghost"]), repo)
    registry = planning.Registry(tmp_path, repo)
    findings = planning.lint_release(rel, registry)
    assert any("dangling ref" in f and "ghost" in f for f in findings)


def test_cross_repo_cycle_is_detected(tmp_path):
    _clear_cache()
    appco = _repo(tmp_path, "appco")
    sharedlib = _repo(tmp_path, "sharedlib")
    (appco / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: appco\n  - alias: sharedlib\n", encoding="utf-8")
    ga = _spec_dir(appco, "a")
    tb = _spec_dir(sharedlib, "b")
    # appco/a depends on sharedlib/b; sharedlib/b depends back on appco/a -> a cycle the per-repo
    # detector cannot see.
    ga.joinpath("dependencies.yaml").write_text(
        "dependencies:\n  - spec: sharedlib/b\n    kind: required\n", encoding="utf-8")
    tb.joinpath("dependencies.yaml").write_text(
        "dependencies:\n  - spec: appco/a\n    kind: required\n", encoding="utf-8")
    _release(appco, "rel", ["appco/a", "sharedlib/b"])
    registry = planning.Registry(tmp_path, appco)
    cycles = planning.cross_repo_cycles(planning.load_releases(appco), registry)
    assert cycles, "a appco<->sharedlib cycle must be reported"


def test_no_false_cycle_on_a_dag(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    a = _spec_dir(repo, "a")
    _spec_dir(repo, "b")
    a.joinpath("dependencies.yaml").write_text(
        "dependencies:\n  - spec: b\n    kind: required\n", encoding="utf-8")
    _release(repo, "rel", ["a", "b"])
    registry = planning.Registry(tmp_path, repo)
    assert planning.cross_repo_cycles(planning.load_releases(repo), registry) == []


# ---------------------------------------------------------------- release scaffolder

def test_release_create_writes_product_release_and_planned_stubs(tmp_path):
    _clear_cache()
    repo = tmp_path / "demo"
    rc = planning.main(["create", "next-roadmap", "--intents", "next-roadmap-intent", "--specs", "a,b,c",
                        "--title", "Next roadmap", "--root", str(repo)])
    assert rc == 0

    product = planning._safe_load(repo / ".builder" / "product.yaml")
    release = planning._safe_load(repo / ".builder" / "releases" / "next-roadmap.yaml")
    assert product["product"] == "demo" and product["repos"] == [{"alias": "demo"}]
    assert release == {"release": "next-roadmap", "product": "demo",
                       "title": "Next roadmap", "status": "draft", "intents": ["next-roadmap-intent"]}
    for spec_id in ("a", "b", "c"):
        stub = planning._safe_load(repo / ".builder" / "specs" / spec_id / "spec.yaml")
        assert stub == {"id": spec_id, "title": spec_id, "status": "planned"}
        assert "release" not in stub


def test_release_create_preserves_built_spec_and_refuses_existing_release(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path, "demo")
    built = _spec_dir(repo, "built", status="verified") / "spec.yaml"
    original = built.read_text(encoding="utf-8")
    rc = planning.main(["create", "roadmap", "--intents", "roadmap-intent", "--specs", "built,new", "--root", str(repo)])
    assert rc == 0
    assert built.read_text(encoding="utf-8") == original
    assert planning._safe_load(repo / ".builder" / "specs" / "new" / "spec.yaml")["status"] == "planned"

    release_path = repo / ".builder" / "releases" / "roadmap.yaml"
    release_original = release_path.read_text(encoding="utf-8")
    rc = planning.main(["create", "roadmap", "--intents", "roadmap-intent", "--specs", "replacement", "--root", str(repo)])
    assert rc == 2
    assert release_path.read_text(encoding="utf-8") == release_original
    assert not (repo / ".builder" / "specs" / "replacement").exists()


def test_release_create_rejects_traversing_spec_before_writing_anything(tmp_path):
    _clear_cache()
    repo = tmp_path / "demo"
    outside = tmp_path / "escape"
    rc = planning.main(["create", "roadmap", "--intents", "roadmap-intent", "--specs", "good,../../escape",
                        "--root", str(repo)])
    assert rc == 2
    assert not (repo / ".builder").exists()
    assert not outside.exists()


def test_release_create_refuses_to_write_through_a_dangling_symlinked_release(tmp_path):
    # A pre-planted symlink whose target does not yet exist slips past `.exists()`
    # (a broken symlink reports False); an unguarded write would follow it and
    # CREATE the target outside .builder/. The scaffolder must refuse.
    _clear_cache()
    repo = tmp_path / "demo"
    outside = tmp_path / "out.yaml"
    releases = repo / ".builder" / "releases"
    releases.mkdir(parents=True)
    (releases / "roadmap.yaml").symlink_to(outside)
    rc = planning.main(["create", "roadmap", "--intents", "roadmap-intent", "--specs", "a", "--root", str(repo)])
    assert rc == 2
    assert not outside.exists()
    assert not (repo / ".builder" / "specs").exists()


def test_release_create_refuses_to_write_through_a_dangling_symlinked_product(tmp_path):
    _clear_cache()
    repo = tmp_path / "demo"
    outside = tmp_path / "out.yaml"
    builder = repo / ".builder"
    builder.mkdir(parents=True)
    (builder / "product.yaml").symlink_to(outside)
    rc = planning.main(["create", "roadmap", "--intents", "roadmap-intent", "--specs", "a", "--root", str(repo)])
    assert rc == 2
    assert not outside.exists()


# ---------------------------------------------------------------- ship is human-only & gated

def test_ship_refuses_an_incomplete_release(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "a"); _spec_dir(repo, "b")
    _release(repo, "rel", ["a", "b"], status="active")
    _stub_scan(repo, {"a": "host-verified", "b": "self-reported"})

    class Args:
        release_id = "rel"; root = str(repo); projects_root = str(tmp_path)
    rc = planning.cmd_release_ship(Args())
    assert rc == 1  # 1/2 verified -> not shippable
    # and the file was NOT transitioned
    assert "shipped" not in (repo / ".builder" / "releases" / "rel.yaml").read_text()


def test_ship_refuses_a_release_with_planned_members(tmp_path):
    _clear_cache()
    repo = _repo(tmp_path)
    _spec_dir(repo, "future", status="planned")
    _release(repo, "rel", ["future"], status="active")
    _stub_scan(repo, {"future": "host-verified"})

    class Args:
        release_id = "rel"; root = str(repo); projects_root = str(tmp_path)
    rc = planning.cmd_release_ship(Args())
    assert rc == 1
    assert "shipped" not in (repo / ".builder" / "releases" / "rel.yaml").read_text()
