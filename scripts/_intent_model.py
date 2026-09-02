from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _yaml import yaml

DECLARED_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
VISIBLE_TERMINAL = {"rejected", "superseded"}
PRE_IMPLEMENTATION_STATUSES = {"specifying", "specified", "spec-reviewed", "designed", "reviewed", "planned"}
IMPLEMENTATION_OR_LATER_STATUSES = {
    "implementing",
    "implemented",
    "adversarially-reviewed",
    "verifying",
    "verified",
    "verified_with_tasks",
    "archived",
    "syncing",
    "synced",
}
DELTA_CATEGORIES = ("capabilities", "behaviors", "journeys")
DELTA_CHANGES = {"create", "enrich", "rewire"}


@dataclass(frozen=True)
class IntentCriterion:
    id: str
    statement: str


@dataclass(frozen=True)
class IntentDelta:
    target: str
    change: str


@dataclass(frozen=True)
class IntentObject:
    path: Path
    repo_relpath: str
    intent: str
    title: str
    status: str
    problem: str
    why: str
    success_criteria: tuple[IntentCriterion, ...]
    non_goals: tuple[str, ...]
    ssot_delta: dict[str, tuple[IntentDelta, ...]]
    specs: tuple[str, ...]
    reason: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True)
class IntentFileDiagnostic:
    path: str
    findings: tuple[str, ...]


@dataclass(frozen=True)
class IntentMemberState:
    ref: str
    canonical_ref: str
    resolved: bool
    status: str | None
    verification: str | None
    finding: str | None = None


@dataclass(frozen=True)
class VisibleIntent:
    intent: IntentObject | None
    path: str
    visible_state: str
    findings: tuple[str, ...]
    members: tuple[IntentMemberState, ...]


@dataclass(frozen=True)
class BacklogCapabilityOwner:
    target: str
    change: str
    intent_id: str
    release_id: str
    visible_state: str
    intent_path: str


@dataclass(frozen=True)
class BacklogCapabilityOwners:
    rows: tuple[BacklogCapabilityOwner, ...]
    collision_intent_ids: tuple[str, ...]


def _relpath(path: Path, repo_root: Path) -> str:
    try:
        # Keep diagnostics keyed by the lexical repository path even when the
        # final component is a refused symlink that resolves outside the repo.
        return path.absolute().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _validate_intent_path(path: Path, repo_root: Path) -> None:
    """Require the canonical in-repo path and refuse every symlink component.

    Checking only ``intent.yaml`` itself is insufficient: an ``<intent-id>``
    directory symlink can otherwise redirect both reads and CLI replacement writes
    outside the repository.
    """
    if path.parent.name == "specs" or "specs" in path.parts:
        raise ValueError(f"{path}: spec-local intent.yaml is out of scope for backlog intent objects")
    root = repo_root.resolve()

    # Symlink refusal runs BEFORE the layout comparison. It is the check that actually stops
    # redirection, and running it first also means a redirected path reports THAT as its
    # reason instead of being mislabelled a layout error.
    for component in (root / ".builder", root / ".builder" / "intents", path.parent, path):
        try:
            if component.is_symlink():
                raise ValueError(f"{path}: symlinked intent path refused at {component}")
        except OSError as exc:
            raise ValueError(f"{path}: unreadable intent path ({exc})") from exc

    # Compare RESOLVED against RESOLVED. This used to weigh `repo_root.resolve()` against a
    # merely-absolute `path`, so any symlink ABOVE the repo made the two sides disagree and
    # every intent load failed with "must use .builder/intents/<intent-id>/intent.yaml" while
    # the caller was already using exactly that layout -- the message sent you to check the
    # one thing that was not wrong. Where a repo is reached through a symlinked parent --
    # a common shell convenience -- this broke the whole intent layer on every platform; on macOS
    # it also broke 42 of this suite's own cases, `tmp_path` living under a symlinked
    # /var/folders. Symmetry is safe because redirection is refused above, independently.
    expected = root / ".builder" / "intents" / path.parent.name / "intent.yaml"
    if path.resolve() != expected:
        raise ValueError(f"{path}: intent artifact must use .builder/intents/<intent-id>/intent.yaml")
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: intent artifact resolves outside repository") from exc


def _load_yaml_text_strict(raw: str, path: Path) -> dict[str, Any]:
    try:
        if hasattr(yaml, "SafeLoader"):
            class UniqueKeyLoader(yaml.SafeLoader):
                pass

            def construct_mapping(loader, node, deep=False):
                mapping = {}
                for key_node, value_node in node.value:
                    key = loader.construct_object(key_node, deep=deep)
                    try:
                        duplicate = key in mapping
                    except TypeError as exc:
                        raise ValueError(f"{path}: unhashable YAML mapping key {key!r}") from exc
                    if duplicate:
                        raise ValueError(f"{path}: duplicate YAML key {key!r}")
                    mapping[key] = loader.construct_object(value_node, deep=deep)
                return mapping

            UniqueKeyLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                construct_mapping,
            )
            data = yaml.load(raw, Loader=UniqueKeyLoader)
        else:  # bundled zero-dependency compatibility parser
            data = yaml.safe_load(raw)
    except ValueError as exc:
        if str(exc).startswith(f"{path}:"):
            raise
        raise ValueError(f"{path}: malformed YAML ({exc})") from exc
    except Exception as exc:
        raise ValueError(f"{path}: malformed YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def _load_yaml_strict(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: unreadable utf-8 intent file ({exc})") from exc
    except OSError as exc:
        raise ValueError(f"{path}: unreadable intent file ({exc})") from exc
    return _load_yaml_text_strict(raw, path)


def _require_exact_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    non_string = [repr(key) for key in data if not isinstance(key, str)]
    if non_string:
        raise ValueError(f"{context}: mapping key(s) must be strings: {', '.join(non_string)}")
    extra = sorted(set(data) - allowed)
    if extra:
        raise ValueError(f"{context}: unknown key(s): {', '.join(extra)}")


def _non_empty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: required non-empty string")
    return value.strip()


def _parse_criterion(item: Any, index: int, path: Path, seen: set[str]) -> IntentCriterion:
    if not isinstance(item, dict):
        raise ValueError(f"{path}: success_criteria[{index}] must be a mapping")
    _require_exact_keys(item, {"id", "statement"}, f"{path}: success_criteria[{index}]")
    cid = _non_empty(item.get("id"), f"{path}: success_criteria[{index}].id")
    if cid in seen:
        raise ValueError(f"{path}: duplicate success_criteria id: {cid}")
    seen.add(cid)
    statement = _non_empty(item.get("statement"), f"{path}: success_criteria[{index}].statement")
    return IntentCriterion(id=cid, statement=statement)


def _parse_delta_item(item: Any, category: str, index: int, path: Path, seen: set[str]) -> IntentDelta:
    if not isinstance(item, dict):
        raise ValueError(f"{path}: ssot_delta.{category}[{index}] must be a mapping")
    _require_exact_keys(item, {"target", "change"}, f"{path}: ssot_delta.{category}[{index}]")
    target = validate_intent_target(item.get("target"), f"{path}: ssot_delta.{category}[{index}].target")
    if target in seen:
        raise ValueError(f"{path}: duplicate ssot_delta.{category} target: {target}")
    seen.add(target)
    change = _non_empty(item.get("change"), f"{path}: ssot_delta.{category}[{index}].change")
    if change not in DELTA_CHANGES:
        raise ValueError(f"{path}: invalid ssot_delta.{category}[{index}].change: {change}")
    return IntentDelta(target=target, change=change)


def validate_intent_target(value: Any, context: str) -> str:
    return _non_empty(value, context)


def _parse_specs(items: Any, path: Path, parse_ref) -> tuple[str, ...]:
    if not isinstance(items, list):
        raise ValueError(f"{path}: specs must be a list")
    canonical: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(f"{path}: specs[{index}] must be a bare or <alias>/<spec-id> string")
        ref, err = parse_ref(item)
        if err or ref is None:
            raise ValueError(f"{path}: specs[{index}] invalid member ref: {err or item!r}")
        if ref.canonical in seen:
            raise ValueError(f"{path}: duplicate member ref: {ref.canonical}")
        seen.add(ref.canonical)
        canonical.append(ref.canonical)
    return tuple(canonical)


def _parse_intent_data(data: dict[str, Any], path: Path, repo_root: Path, parse_ref) -> IntentObject:
    allowed = {
        "artifact",
        "intent",
        "title",
        "status",
        "problem",
        "why",
        "success_criteria",
        "non_goals",
        "ssot_delta",
        "specs",
        "reason",
        "superseded_by",
    }
    _require_exact_keys(data, allowed, str(path))
    if data.get("artifact") != "intent-object":
        raise ValueError(f"{path}: artifact must be 'intent-object'")
    intent_id = _non_empty(data.get("intent"), f"{path}: intent")
    if intent_id != path.parent.name:
        raise ValueError(f"{path}: intent id {intent_id!r} does not match parent directory {path.parent.name!r}")
    title = _non_empty(data.get("title"), f"{path}: title")
    status = _non_empty(data.get("status"), f"{path}: status")
    if status not in DECLARED_STATUSES:
        raise ValueError(f"{path}: invalid status: {status}")
    problem = _non_empty(data.get("problem"), f"{path}: problem")
    why = _non_empty(data.get("why"), f"{path}: why")

    sc = data.get("success_criteria")
    if not isinstance(sc, list) or not sc:
        raise ValueError(f"{path}: success_criteria must be a non-empty list")
    seen_criteria: set[str] = set()
    success_criteria = tuple(_parse_criterion(item, index, path, seen_criteria) for index, item in enumerate(sc))

    non_goals_raw = data.get("non_goals")
    if not isinstance(non_goals_raw, list) or not non_goals_raw:
        raise ValueError(f"{path}: non_goals must be a non-empty list")
    non_goals = tuple(_non_empty(item, f"{path}: non_goals[{index}]") for index, item in enumerate(non_goals_raw))

    delta_raw = data.get("ssot_delta")
    if not isinstance(delta_raw, dict):
        raise ValueError(f"{path}: ssot_delta must be a mapping")
    _require_exact_keys(delta_raw, set(DELTA_CATEGORIES), f"{path}: ssot_delta")
    ssot_delta: dict[str, tuple[IntentDelta, ...]] = {}
    for category in DELTA_CATEGORIES:
        items = delta_raw.get(category)
        if not isinstance(items, list):
            raise ValueError(f"{path}: ssot_delta.{category} must be a list")
        seen_targets: set[str] = set()
        ssot_delta[category] = tuple(
            _parse_delta_item(item, category, index, path, seen_targets)
            for index, item in enumerate(items)
        )

    specs = _parse_specs(data.get("specs"), path, parse_ref)
    reason = data.get("reason")
    superseded_by = data.get("superseded_by")
    if status in VISIBLE_TERMINAL:
        reason = _non_empty(reason, f"{path}: reason")
    elif reason is not None:
        raise ValueError(f"{path}: reason allowed only for rejected or superseded")
    if status == "superseded":
        if superseded_by is not None:
            superseded_by = _non_empty(superseded_by, f"{path}: superseded_by")
    elif superseded_by is not None:
        raise ValueError(f"{path}: superseded_by allowed only for superseded")

    return IntentObject(
        path=path,
        repo_relpath=_relpath(path, repo_root),
        intent=intent_id,
        title=title,
        status=status,
        problem=problem,
        why=why,
        success_criteria=success_criteria,
        non_goals=non_goals,
        ssot_delta=ssot_delta,
        specs=specs,
        reason=reason,
        superseded_by=superseded_by,
    )


def load_intent_object(path: Path, repo_root: Path, parse_ref) -> IntentObject:
    _validate_intent_path(path, repo_root)
    return _parse_intent_data(_load_yaml_strict(path), path, repo_root, parse_ref)


def validate_intent_payload(payload: bytes, path: Path, repo_root: Path, parse_ref) -> IntentObject:
    """Validate mutation bytes before the single atomic replacement occurs."""
    _validate_intent_path(path, repo_root)
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: unreadable utf-8 intent payload ({exc})") from exc
    return _parse_intent_data(_load_yaml_text_strict(raw, path), path, repo_root, parse_ref)


def load_repo_intents(repo_root: Path, parse_ref) -> tuple[list[IntentObject], list[IntentFileDiagnostic]]:
    intents_dir = repo_root / ".builder" / "intents"
    if not intents_dir.is_dir():
        return [], []
    intents: list[IntentObject] = []
    diagnostics: list[IntentFileDiagnostic] = []
    try:
        paths = sorted(
            child / "intent.yaml"
            for child in intents_dir.iterdir()
            if child.is_dir() or child.is_symlink()
        )
    except OSError as exc:
        relpath = _relpath(intents_dir, repo_root)
        return [], [IntentFileDiagnostic(path=relpath, findings=(f"{relpath}: unreadable intent inventory ({exc})",))]
    declared_ids: dict[Path, str] = {}
    id_paths: dict[str, list[str]] = {}
    for path in paths:
        try:
            _validate_intent_path(path, repo_root)
            raw_data = _load_yaml_strict(path)
        except ValueError:
            continue
        raw_id = raw_data.get("intent")
        if isinstance(raw_id, str) and raw_id.strip():
            declared_id = raw_id.strip()
            declared_ids[path] = declared_id
            id_paths.setdefault(declared_id, []).append(_relpath(path, repo_root))
    for path in paths:
        relpath = _relpath(path, repo_root)
        findings: list[str] = []
        declared_id = declared_ids.get(path)
        duplicate_paths = id_paths.get(declared_id, []) if declared_id is not None else []
        if len(duplicate_paths) > 1:
            findings.append(
                f"{relpath}: duplicate intent id {declared_id!r}; declared at {', '.join(duplicate_paths)}"
            )
        try:
            intent = load_intent_object(path, repo_root, parse_ref)
        except ValueError as exc:
            findings.append(str(exc))
            diagnostics.append(IntentFileDiagnostic(path=relpath, findings=tuple(findings)))
            continue
        if findings:
            diagnostics.append(IntentFileDiagnostic(path=relpath, findings=tuple(findings)))
            continue
        intents.append(intent)
    return intents, diagnostics


def project_visible_state(intent: IntentObject, members: list[IntentMemberState]) -> VisibleIntent:
    findings = [member.finding for member in members if member.finding]
    if intent.status != "accepted":
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state=intent.status, findings=tuple(findings), members=tuple(members))
    if not intent.specs:
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="accepted", findings=tuple(findings), members=tuple(members))
    if len(members) != len(intent.specs):
        findings.append("accepted intent member state is incomplete")
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="accepted", findings=tuple(findings), members=tuple(members))
    if findings:
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="accepted", findings=tuple(findings), members=tuple(members))
    if all(member.verification == "host-verified" and member.status == "synced" for member in members):
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="fulfilled", findings=tuple(findings), members=tuple(members))
    if any((member.status or "") in IMPLEMENTATION_OR_LATER_STATUSES for member in members):
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="in-flight", findings=tuple(findings), members=tuple(members))
    if all((member.status or "") in PRE_IMPLEMENTATION_STATUSES for member in members):
        return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="decomposed", findings=tuple(findings), members=tuple(members))
    findings.append("accepted intent has unreadable, dangling, or unrecognized member status")
    return VisibleIntent(intent=intent, path=intent.repo_relpath, visible_state="accepted", findings=tuple(findings), members=tuple(members))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    directory = path.parent
    try:
        original_mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        # New sidecars need the same atomic publication guarantee as replacements.
        # Keep their initial permissions private; callers can widen them explicitly.
        original_mode = 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, original_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
