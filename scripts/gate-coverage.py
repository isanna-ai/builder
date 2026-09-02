#!/usr/bin/env python3
"""Audit Builder gate coverage and verification stamps.

python3 scripts/gate-coverage.py [--root .] [--json] [--check] [--verify-chain] [--queue-root PATH]
python3 scripts/gate-coverage.py --all DIR [--json] [--check] [--verify-chain]

Recommended CI line:
python3 scripts/gate-coverage.py --root . --check --verify-chain

A 100%-done release with 40% gate coverage is a confession.

Every ``phase-complete`` implement turn shipped code into the final tree; a
stamp that only audits the last verify turn would bless a tree assembled from
unaudited acceptances. Worst-case wins.

host-verified = on every host-accepted implement/verify turn of this spec, the host itself ran the verify
commands and saw exit 0 (and, on implement turns, confirmed a real source change). It is an
**observational** stamp from host-recorded gate outcomes; it does not encode the warn/enforce posture
(J1), and it says nothing about turns that predate gate evidence (those force `unknown`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from _yaml import yaml
_YAML_IS_PYYAML = hasattr(yaml, "SafeLoader")
_SHIM_NOTICE_PRINTED = False
_CHAIN_IMPORT_NOTICE_PRINTED = False
_CHAIN_SHIM_NOTICE_PRINTED = False

GATE_NAMES = ("host_verify", "source_diff", "red_baseline", "packet_contract")
GATE_PHASES = {
    "host_verify": {"implement", "verify"},
    "source_diff": {"implement"},
    "red_baseline": {"plan"},
    "packet_contract": {"implement"},
}
GATE_NOT_APPLICABLE = {
    "host_verify": {"turn_incomplete", "non_gated_phase"},
    "source_diff": {"turn_incomplete", "non_gated_phase"},
    "red_baseline": {"turn_incomplete", "non_gated_phase", "no_tasks", "no_tdd_tasks"},
    "packet_contract": {"turn_incomplete", "non_gated_phase", "no_packet"},
}
COMPLETED_CLAIMS = {"verified", "verified_with_tasks", "synced", "archived"}
CHECK_FINDINGS = {"self-certified", "host-contradicted", "chain-violation"}
CHAIN_NOTE = (
    '  note: the chain is tamper-EVIDENT, not tamper-proof. "intact" proves the on-disk record is\n'
    "  internally consistent with what the host wrote; it does not prove the files were never\n"
    "  rewritten by the same OS user. The verdicts themselves were computed by the host at gate\n"
    "  time — that, not this file check, is the load-bearing guarantee."
)
BUNDLE_NAME_RE = re.compile(r"^\d{4,}-[A-Za-z0-9_]+-[A-Za-z0-9_]+\.yaml$")
KNOWN_NON_RECORD_SUFFIXES = {".log", ".tmp"}


class OperationalError(RuntimeError):
    """A filesystem failure that makes a fail-closed audit impossible."""


@dataclass
class Turn:
    attempt_id: str
    spec_id: str
    phase: str
    decision: str
    outcome: str
    created_at: str
    gates: dict | None
    gate_evidence: list


@dataclass(frozen=True)
class FsRead:
    """Result of a filesystem read: optional absence, usable data, or blindness."""

    state: str
    path: Path
    value: object = None
    detail: str = ""
    kind: str = ""


def normalize_phase(raw) -> str:
    return re.sub(r"^\d+-", "", str(raw or "")).strip()


def outcome_token(reason) -> str:
    matches = re.findall(r"outcome:\s*([A-Za-z_]+)", str(reason or ""))
    return matches[-1].upper() if matches else ""


def _shim_notice() -> None:
    global _SHIM_NOTICE_PRINTED
    if not _YAML_IS_PYYAML and not _SHIM_NOTICE_PRINTED:
        print(
            "note: PyYAML not installed — using the repo's lossy YAML shim; long wrapped fields may be misread",
            file=sys.stderr,
        )
        _SHIM_NOTICE_PRINTED = True


def _resolve_path(path: Path, *, strict: bool) -> Path:
    try:
        return Path(path).resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise OperationalError(f"cannot resolve path {path}: {exc}") from exc


def _probe_path(
    path: Path,
    expected: str,
    *,
    allow_symlink: bool = False,
    containment: Path | None = None,
) -> FsRead:
    """Classify one filesystem artifact without collapsing anomalies into absence.

    ``missing`` is reserved for a genuinely absent optional path. A broken or
    forbidden symlink, an escaping target, an unexpected type, or inaccessible
    metadata is always ``blind``. Callers must either record that blindness or
    turn it into an operational failure.
    """
    path = Path(path)
    try:
        metadata = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return FsRead("missing", path)
    except OSError as exc:
        return FsRead("blind", path, detail=f"cannot inspect {path}: {exc}")

    inspected = path
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        if not allow_symlink:
            return FsRead("blind", path, detail=f"expected real {expected}, found symlink: {path}")
        try:
            inspected = path.resolve(strict=True)
            mode = inspected.stat().st_mode
        except (OSError, RuntimeError) as exc:
            return FsRead("blind", path, detail=f"cannot resolve symlink {path}: {exc}")
        if containment is not None:
            try:
                inspected.relative_to(containment)
            except ValueError:
                return FsRead("blind", path, detail=f"symlink escapes {containment}: {path}")

    kind = "directory" if stat.S_ISDIR(mode) else "regular file" if stat.S_ISREG(mode) else "other"
    if expected == "entry":
        matches = kind in {"directory", "regular file"}
    else:
        matches = kind == expected
    if not matches:
        return FsRead("blind", path, detail=f"expected {expected}, found another filesystem type: {path}")
    return FsRead("ready", path, value=inspected, kind=kind)


def _read_text_file(
    path: Path,
    *,
    allow_symlink: bool = False,
    containment: Path | None = None,
) -> FsRead:
    probe = _probe_path(
        path,
        "regular file",
        allow_symlink=allow_symlink,
        containment=containment,
    )
    if probe.state != "ready":
        return probe
    try:
        with Path(probe.value).open(encoding="utf-8") as fh:
            return FsRead("ready", Path(path), value=fh.read())
    except (OSError, UnicodeError) as exc:
        return FsRead("blind", Path(path), detail=f"cannot read {path}: {exc}")


def _load_yaml_file(
    path: Path,
    *,
    allow_symlink: bool = False,
    containment: Path | None = None,
) -> FsRead:
    text = _read_text_file(path, allow_symlink=allow_symlink, containment=containment)
    if text.state != "ready":
        return text
    data = _load_yaml_text(str(text.value))
    if data is None:
        return FsRead("blind", Path(path), detail=f"cannot parse YAML mapping: {path}")
    return FsRead("ready", Path(path), value=data)


def _load_yaml_text(text: str) -> dict | None:
    try:
        _shim_notice()
        if not _YAML_IS_PYYAML and text.lstrip().startswith(("{", "[")):
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _dir_entries(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise OperationalError(f"cannot enumerate directory {path}: {exc}") from exc


def _dedupe(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            out.append(path)
            seen.add(key)
    return out


def _queue_candidates(root, override) -> tuple[list[Path], Path | None, list[dict]]:
    root = _resolve_path(Path(root), strict=False)
    if override is not None:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return [candidate], None, []

    declared = None
    blindness: list[dict] = []
    config_read = _load_yaml_file(runtime_dir(root) / "dispatch.yaml")
    if config_read.state == "blind":
        blindness.append(_blindness("dispatch-config-unreadable", config_read.detail))
    config = config_read.value if config_read.state == "ready" else None
    if isinstance(config, dict):
        store = config.get("queue_store")
        if isinstance(store, dict) and isinstance(store.get("path"), str):
            declared = Path(store["path"])
            if not declared.is_absolute():
                declared = root / declared

    candidates: list[Path] = []
    if declared is not None:
        candidates.append(declared)
        for suffix in (".sqlite3", ".sqlite", ".db"):
            if declared.name.endswith(suffix):
                candidates.append(declared.with_name(declared.name[: -len(suffix)]))
    # Root-relative candidates are ALWAYS considered, never suppressed by `declared`.
    # `dispatch.yaml` may record an ABSOLUTE queue path written on a different machine, or inside a
    # container, so `declared` can resolve to a path that does not exist here. Deriving the fallbacks
    # from declared.parent inherited that dead prefix, so the real queue -- sitting right next to the
    # config, with dozens of attempts in it -- was never even a candidate, and the tool reported "no
    # dispatch history" for every repo. An audit tool that silently sees nothing is worse than none.
    parents = []
    if declared is not None:
        parents.append(declared.parent)
    parents.append(runtime_dir(root))
    for parent in parents:
        candidates.extend([parent / "dispatch-queue", parent / "dispatch", parent / "dispatch-queue.sqlite3"])
    return _dedupe(candidates), declared, blindness


def _resolve_queue_roots_audit(root, override) -> tuple[list[Path], str, int, list[dict]]:
    """Resolve queue roots while retaining every anomalous candidate read."""
    candidates, declared, blindness = _queue_candidates(root, override)
    existing: list[Path] = []
    for candidate in candidates:
        candidate_probe = _probe_path(candidate, "directory", allow_symlink=True)
        if candidate_probe.state == "missing":
            continue
        if candidate_probe.state == "blind":
            blindness.append(_blindness("queue-root-unreadable", candidate_probe.detail))
            continue

        queue_root = Path(candidate_probe.value)
        queue_probe = _probe_path(queue_root / "queue", "directory", allow_symlink=True)
        if queue_probe.state == "missing":
            continue
        if queue_probe.state == "blind":
            blindness.append(_blindness("queue-directory-unreadable", queue_probe.detail))
            continue

        existing.append(queue_root)
        attempts_dir = Path(queue_probe.value) / "attempts"
        # allow_symlink: a queue/attempts symlinked to a real, readable directory is a LEGITIMATE
        # layout (the `queue/` dir above already allows it). Only a BROKEN symlink or a wrong type
        # is blindness. Flagging a healthy symlink cries wolf -- and an audit tool that cries wolf
        # gets muted, which is as bad as the silence it was built to fix.
        attempts_probe = _probe_path(attempts_dir, "directory", allow_symlink=True)
        if attempts_probe.state == "missing":
            continue
        if attempts_probe.state == "blind":
            blindness.append(_blindness("queue-attempts-unreadable", attempts_probe.detail))
            continue
        # Prove enumeration now. An unreadable attempts directory must never
        # masquerade as an empty candidate.
        _dir_entries(Path(attempts_probe.value))

    existing = _dedupe(existing)
    if not existing:
        source = "missing"
    elif override is not None:
        source = "override"
    elif len(existing) > 1:
        source = "candidates"
    elif declared is not None and existing[0] == _resolve_path(declared, strict=False):
        source = "config"
    else:
        source = "fallback"
    return existing, source, len(candidates), _dedupe_blindness(blindness)


def resolve_queue_roots(root, override) -> tuple[list[Path], str, int]:
    """Return every existing candidate queue root; no candidate is scored away."""
    existing, source, count, blindness = _resolve_queue_roots_audit(root, override)
    if blindness:
        details = "; ".join(item["detail"] for item in blindness)
        raise OperationalError(f"queue discovery is blind: {details}")
    return existing, source, count


def resolve_queue_root(root, override) -> tuple[Path | None, str, int]:
    """Compatibility helper; scans use :func:`resolve_queue_roots`."""
    roots, source, n_candidates = resolve_queue_roots(root, override)
    for queue_root in roots:
        attempts_dir = queue_root / "queue" / "attempts"
        # allow_symlink: must match the scanner path exactly. A healthy symlinked attempts dir raised
        # OperationalError here while the scanner accepted it -- the same false alarm, one door over.
        probe = _probe_path(attempts_dir, "directory", allow_symlink=True)
        if probe.state == "blind":
            raise OperationalError(probe.detail)
        if probe.state == "ready" and _dir_entries(Path(probe.value)):
            return queue_root, source, n_candidates
    return (roots[0] if roots else None), source, n_candidates


def _blindness(klass: str, detail: str, specs: list[str] | None = None) -> dict:
    # None => global blindness (applies to every spec, "*"); an EMPTY list => blindness that
    # belongs to no particular spec (e.g. an unbindable attempt) and must not taint any spec's
    # verdict. `specs or ["*"]` would wrongly coerce [] back to global, so distinguish explicitly.
    scoped = ["*"] if specs is None else sorted(set(specs))
    return {"class": klass, "detail": detail, "specs": scoped}


def _dedupe_blindness(items: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for item in items:
        key = (item["class"], item["detail"], tuple(item.get("specs", [])))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _turn_from_data(path: Path, data: dict) -> Turn | None:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    spec_id = metadata.get("spec_id")
    if not spec_id:
        return None
    gates = metadata.get("gates")
    raw_evidence = metadata.get("gate_evidence")
    if raw_evidence is None:
        gate_evidence = []
    elif isinstance(raw_evidence, list):
        gate_evidence = raw_evidence
    else:
        # Preserve the malformed value so --verify-chain can report it.
        gate_evidence = [raw_evidence]
    return Turn(
        attempt_id=str(data.get("attempt_id") or path.stem),
        spec_id=str(spec_id),
        phase=normalize_phase(metadata.get("phase")),
        decision=str(metadata.get("decision") or ""),
        outcome=outcome_token(metadata.get("reason")),
        created_at=str(data.get("created_at") or metadata.get("started_at") or ""),
        gates=gates if isinstance(gates, dict) else None,
        gate_evidence=gate_evidence,
    )


def _load_turns_from_roots(queue_roots: list[Path]) -> tuple[list[Turn], dict, list[dict]]:
    diagnostics = {"unreadable_attempts": 0, "attempts_without_spec_id": 0}
    blindness: list[dict] = []
    records: dict[str, tuple[Turn, str, Path]] = {}
    for queue_root in queue_roots:
        attempts_dir = Path(queue_root) / "queue" / "attempts"
        # allow_symlink: a queue/attempts symlinked to a real, readable directory is a LEGITIMATE
        # layout (the `queue/` dir above already allows it). Only a BROKEN symlink or a wrong type
        # is blindness. Flagging a healthy symlink cries wolf -- and an audit tool that cries wolf
        # gets muted, which is as bad as the silence it was built to fix.
        attempts_probe = _probe_path(attempts_dir, "directory", allow_symlink=True)
        if attempts_probe.state == "missing":
            continue
        if attempts_probe.state == "blind":
            blindness.append(_blindness("queue-attempts-unreadable", attempts_probe.detail))
            continue
        for path in _dir_entries(Path(attempts_probe.value)):
            entry_probe = _probe_path(path, "regular file")
            if entry_probe.state != "ready":
                detail = entry_probe.detail or f"attempt entry disappeared during scan: {path}"
                blindness.append(_blindness("unexpected-attempt-entry", detail))
                continue
            suffix = path.suffix.lower()
            if suffix in KNOWN_NON_RECORD_SUFFIXES:
                continue
            if suffix not in (".yaml", ".yml", ".json"):
                blindness.append(_blindness("unknown-attempt-extension", str(path)))
                continue
            text_read = _read_text_file(path)
            if text_read.state != "ready":
                diagnostics["unreadable_attempts"] += 1
                blindness.append(_blindness("unreadable-attempt", text_read.detail or str(path)))
                continue
            text = str(text_read.value)
            data = _load_yaml_text(text)
            if data is None:
                diagnostics["unreadable_attempts"] += 1
                blindness.append(_blindness("unparseable-attempt", str(path)))
                continue
            turn = _turn_from_data(path, data)
            if turn is None:
                diagnostics["attempts_without_spec_id"] += 1
                # A parseable attempt with no spec_id is a pre-binding artifact (a lane that failed
                # before it bound to a spec -- e.g. a 401'd claude-lane attempt). It belongs to NO
                # spec, so scope it to [] rather than global "*": it stays visible at the repo level
                # and in the diagnostics counter, but must not downgrade every spec's host-verified
                # verdict. (Two such stubs once zeroed the whole roadmap's fulfillment.)
                blindness.append(_blindness("attempt-without-spec-id", str(path), []))
                continue
            fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
            previous = records.get(turn.attempt_id)
            if previous is None:
                records[turn.attempt_id] = (turn, fingerprint, path)
                continue
            previous_turn, previous_fingerprint, previous_path = previous
            if fingerprint != previous_fingerprint:
                blindness.append(
                    _blindness(
                        "ambiguous-attempt",
                        f"attempt_id {turn.attempt_id!r} differs between {previous_path} and {path}",
                        [previous_turn.spec_id, turn.spec_id],
                    )
                )
    return [record[0] for record in records.values()], diagnostics, _dedupe_blindness(blindness)


def load_turns(queue_root) -> tuple[list[Turn], dict]:
    roots = [] if queue_root is None else [Path(queue_root)]
    turns, diagnostics, _ = _load_turns_from_roots(roots)
    return turns, diagnostics


def coverage(turns) -> dict[str, dict[str, int]]:
    result = {
        gate: {"adjudicated": 0, "claimed": 0, "unknown": 0, "not_applicable": 0}
        for gate in GATE_NAMES
    }
    for turn in turns:
        for gate in GATE_NAMES:
            if turn.phase not in GATE_PHASES[gate]:
                continue
            if turn.gates is None or not isinstance(turn.gates.get(gate), str):
                result[gate]["unknown"] += 1
                continue
            token = turn.gates[gate]
            if token.startswith("abstain:") and token.split(":", 1)[1] in GATE_NOT_APPLICABLE[gate]:
                result[gate]["not_applicable"] += 1
                continue
            result[gate]["claimed"] += 1
            if token == "pass" or token.startswith("fail:"):
                result[gate]["adjudicated"] += 1
    return result


def classify_accepted_turn(t):
    if t.gates is None:
        return "unknown"
    hv = t.gates.get("host_verify")
    sd = t.gates.get("source_diff")
    if not isinstance(hv, str) or (t.phase == "implement" and not isinstance(sd, str)):
        return "unknown"
    if hv.startswith("fail:") or (t.phase == "implement" and sd.startswith("fail:")):
        return "contradicted"
    if hv == "abstain:no_commands":
        return "self_certified"
    if hv == "pass" and (t.phase != "implement" or sd == "pass"):
        return "verified"
    return "unadjudicated"


def _finding_for_turn(klass: str, turn: Turn, detail: str) -> dict:
    return {"class": klass, "attempt_id": turn.attempt_id, "phase": turn.phase, "detail": detail}


def _unadjudicated_finding(turn: Turn) -> dict:
    hv = turn.gates.get("host_verify") if turn.gates else None
    sd = turn.gates.get("source_diff") if turn.gates else None
    if hv == "abstain:off":
        return _finding_for_turn("gates-off", turn, "host_verify=abstain:off")
    detail = hv if isinstance(hv, str) and hv != "pass" else sd
    return _finding_for_turn("gate-degraded", turn, str(detail or "unknown"))


def _contradicted_detail(turn: Turn) -> str:
    parts = []
    if turn.gates:
        hv = turn.gates.get("host_verify")
        sd = turn.gates.get("source_diff")
        if isinstance(hv, str) and hv.startswith("fail:"):
            parts.append(f"host_verify={hv}")
        if turn.phase == "implement" and isinstance(sd, str) and sd.startswith("fail:"):
            parts.append(f"source_diff={sd}")
    return " ".join(parts) if parts else "host gate failed"


def stamp_spec(spec_id, acc, claim) -> dict:
    acc = sorted(acc, key=lambda t: (t.created_at, t.attempt_id))
    terminal = [t for t in acc if t.phase == "verify" and t.outcome != "VERIFIED_WITH_TASKS"]
    tv = terminal[-1] if terminal else None
    findings: list[dict] = []
    rework_loops = sum(1 for t in acc if t.phase == "verify" and t.outcome == "VERIFIED_WITH_TASKS")

    classified = [(turn, classify_accepted_turn(turn)) for turn in acc]

    if tv is None and claim not in COMPLETED_CLAIMS:
        verification = None
    elif tv is None and claim in COMPLETED_CLAIMS:
        verification = "unknown"
        findings.append(
            {
                "class": "no-host-record",
                "attempt_id": "-",
                "phase": "-",
                "detail": f"spec claims '{claim}' without a terminal host-accepted verify turn",
            }
        )
        # A completion claim makes accepted pathology reportable even when the
        # terminal verify record is absent. The stamp stays conservatively unknown.
        for turn, klass in classified:
            if klass == "contradicted":
                findings.append(_finding_for_turn("host-contradicted", turn, _contradicted_detail(turn)))
            elif klass == "self_certified":
                findings.append(_finding_for_turn("self-certified", turn, "host_verify=abstain:no_commands"))
            elif klass == "unadjudicated":
                findings.append(_unadjudicated_finding(turn))
    else:
        for turn, klass in classified:
            if klass == "contradicted":
                findings.append(_finding_for_turn("host-contradicted", turn, _contradicted_detail(turn)))
            elif klass == "self_certified":
                findings.append(_finding_for_turn("self-certified", turn, "host_verify=abstain:no_commands"))
            elif klass == "unadjudicated":
                findings.append(_unadjudicated_finding(turn))

        classes = [klass for _, klass in classified]
        # Precedence rationale: known-bad beats unknowable.
        if "contradicted" in classes or "self_certified" in classes or "unadjudicated" in classes:
            verification = "self-reported"
        elif "unknown" in classes:
            verification = "unknown"
        else:
            verification = "host-verified"

    return {
        "spec": spec_id,
        "claim": claim,
        "verification": verification,
        "accepted_turns": len(acc),
        "unknown_turns": sum(1 for t in acc if classify_accepted_turn(t) == "unknown"),
        "rework_loops": rework_loops,
        "findings": findings,
        "blindness": [],
        "chain": {"checked": False},
    }


def _spec_dirs(root: Path) -> tuple[dict[str, Path], list[dict]]:
    out: dict[str, Path] = {}
    blindness: list[dict] = []
    specs_root = runtime_dir(root) / "specs"
    root_probe = _probe_path(specs_root, "directory")
    if root_probe.state == "missing":
        return out, blindness
    if root_probe.state == "blind":
        blindness.append(_blindness("specs-directory-unreadable", root_probe.detail))
        return out, blindness
    for entry in _dir_entries(Path(root_probe.value)):
        # Regular files may intentionally live beside spec directories and are
        # not spec candidates. Symlinks and exotic entries are never silently
        # filtered because they can masquerade as directories.
        entry_probe = _probe_path(entry, "entry")
        if entry.name == "archive":
            if entry_probe.state != "ready" or entry_probe.kind != "directory":
                detail = entry_probe.detail or f"archive directory disappeared during scan: {entry}"
                blindness.append(_blindness("spec-archive-unreadable", detail))
                continue
            for archived in _dir_entries(Path(entry_probe.value)):
                archived_probe = _probe_path(archived, "entry")
                if archived_probe.state == "ready" and archived_probe.kind == "regular file":
                    continue
                out.setdefault(archived.name, archived)
                if archived_probe.state != "ready" or archived_probe.kind != "directory":
                    detail = archived_probe.detail or f"archived spec disappeared during scan: {archived}"
                    blindness.append(_blindness("spec-directory-unreadable", detail, [archived.name]))
            continue
        if entry_probe.state == "ready" and entry_probe.kind == "regular file":
            continue
        out.setdefault(entry.name, entry)
        if entry_probe.state != "ready" or entry_probe.kind != "directory":
            detail = entry_probe.detail or f"spec directory disappeared during scan: {entry}"
            blindness.append(_blindness("spec-directory-unreadable", detail, [entry.name]))
    return out, _dedupe_blindness(blindness)


def _spec_claim(spec_dir: Path | None) -> tuple[str, list[dict]]:
    if spec_dir is None:
        return "?", []
    directory = _probe_path(spec_dir, "directory")
    if directory.state != "ready":
        detail = directory.detail or f"spec directory disappeared during scan: {spec_dir}"
        return "?", [_blindness("spec-directory-unreadable", detail)]
    data_read = _load_yaml_file(spec_dir / "spec.yaml")
    if data_read.state == "missing":
        return "?", []
    if data_read.state == "blind":
        return "?", [_blindness("spec-claim-unreadable", data_read.detail)]
    data = data_read.value
    status = data.get("status")
    return (str(status) if status is not None else "?"), []


def _import_gate_evidence():
    try:
        if _SCRIPT_DIR not in sys.path:
            sys.path.insert(0, _SCRIPT_DIR)
        from _dispatch_runtime import gate_evidence
        return gate_evidence
    except Exception:
        return None


def _chain_for_spec(spec_dir: Path | None, acc: list[Turn]) -> tuple[dict, list[dict], list[dict]]:
    global _CHAIN_IMPORT_NOTICE_PRINTED, _CHAIN_SHIM_NOTICE_PRINTED
    if spec_dir is None:
        return {"checked": False}, [], []

    spec_probe = _probe_path(spec_dir, "directory")
    if spec_probe.state != "ready":
        detail = spec_probe.detail or f"spec directory disappeared during chain scan: {spec_dir}"
        return {"checked": False}, [], [_blindness("spec-directory-unreadable", detail)]

    evidence_dir = spec_dir / "gate-evidence"
    has_refs = any(turn.gate_evidence for turn in acc)
    evidence_probe = _probe_path(evidence_dir, "directory")
    if evidence_probe.state == "missing" and not has_refs:
        return {"checked": False}, [], []

    violations: list[str] = []
    findings: list[dict] = []
    blindness: list[dict] = []
    # This logical, unresolved path is the containment anchor. A spec-directory
    # or gate-evidence symlink is rejected above instead of relocating trust.
    evidence_root = evidence_dir
    if evidence_probe.state == "blind":
        detail = f"gate-evidence is not a readable real directory: {evidence_probe.detail}"
        violations.append(detail)
        turn = acc[-1] if acc else Turn("-", "", "", "", "", "", None, [])
        findings.append(_finding_for_turn("chain-violation", turn, detail))
        blindness.append(_blindness("gate-evidence-unreadable", detail))
        return {"checked": False, "bundles": 0, "violations": violations}, findings, blindness

    bundle_count = 0
    if evidence_probe.state == "ready":
        bundle_count = len(_dir_entries(Path(evidence_probe.value)))

    # An EMPTY gate-evidence/ dir with no referenced bundles is "nothing to verify", NOT a violation.
    # verify_chain() reports an empty dir as a violation (correct for ITS caller: a spec that should have
    # bundles and has none). Here it is a healthy state -- gate evidence has simply never been written for
    # this spec. Treating it as a chain-violation made `--check` exit 1 on a clean repo. A false alarm is
    # as damaging as the silence this tool exists to fix.
    referenced = any(turn.gate_evidence for turn in acc)
    if evidence_probe.state == "ready" and bundle_count == 0 and not referenced:
        return {"checked": False, "bundles": 0, "violations": []}, findings, blindness

    for turn in acc:
        for ref in turn.gate_evidence:
            if not (
                isinstance(ref, dict)
                and isinstance(ref.get("path"), str)
                and ref.get("path")
                and isinstance(ref.get("sha256"), str)
                and ref.get("sha256")
            ):
                detail = f"malformed gate_evidence reference: {ref!r}"
                violations.append(detail)
                findings.append(_finding_for_turn("chain-violation", turn, detail))
                continue

            rel = ref["path"]
            rel_path = Path(rel)
            path_error = None
            if rel_path.is_absolute():
                path_error = "absolute path is forbidden"
            elif ".." in rel_path.parts:
                path_error = "parent traversal is forbidden"
            elif not BUNDLE_NAME_RE.fullmatch(rel_path.name):
                path_error = "invalid bundle filename"
            else:
                bundle_path = spec_dir / rel_path
                bundle_probe = _probe_path(
                    bundle_path,
                    "regular file",
                    allow_symlink=True,
                    containment=evidence_root,
                )
                if bundle_probe.state == "missing":
                    path_error = "file is absent"
                elif bundle_probe.state == "blind":
                    if "escapes" in bundle_probe.detail:
                        path_error = "path escapes gate-evidence"
                    else:
                        path_error = bundle_probe.detail
            if path_error is not None:
                detail = f"metadata/bundle cross-check failed: {rel} ({path_error})"
                violations.append(detail)
                findings.append(_finding_for_turn("chain-violation", turn, detail))
                continue

            failed = False
            if _YAML_IS_PYYAML:
                data_read = _load_yaml_file(
                    bundle_path,
                    allow_symlink=True,
                    containment=evidence_root,
                )
                failed = (
                    data_read.state != "ready"
                    or str(data_read.value.get("bundle_sha256") or "") != ref["sha256"]
                )
            if failed:
                detail = f"metadata/bundle cross-check failed: {rel}"
                violations.append(detail)
                findings.append(_finding_for_turn("chain-violation", turn, detail))

    if _YAML_IS_PYYAML:
        gate_evidence = _import_gate_evidence()
        if gate_evidence is None:
            if not _CHAIN_IMPORT_NOTICE_PRINTED:
                print("note: chain verification unavailable (dispatch runtime not importable)", file=sys.stderr)
                _CHAIN_IMPORT_NOTICE_PRINTED = True
            blindness.append(_blindness("chain-unavailable", "dispatch runtime not importable"))
            return {"checked": False, "bundles": bundle_count, "violations": violations}, findings, blindness
        if evidence_probe.state == "ready":
            for violation in gate_evidence.verify_chain(spec_dir):
                violations.append(violation)
                turn = acc[-1] if acc else Turn("-", "", "", "", "", "", None, [])
                findings.append(_finding_for_turn("chain-violation", turn, violation))
        if violations:
            blindness.append(_blindness("chain-verification-failed", "gate-evidence chain has violations"))
        return {"checked": True, "bundles": bundle_count, "violations": violations}, findings, blindness

    if not _CHAIN_SHIM_NOTICE_PRINTED:
        print(
            "chain sha recompute skipped (requires PyYAML); cross-link existence check only",
            file=sys.stderr,
        )
        _CHAIN_SHIM_NOTICE_PRINTED = True
    blindness.append(_blindness("chain-hash-unverified", "hash chain requires PyYAML"))
    if violations:
        blindness.append(_blindness("chain-verification-failed", "gate-evidence cross-links have violations"))
    return {"checked": "partial", "bundles": bundle_count, "violations": violations}, findings, blindness


def _canonicalize_turn_specs(turns: list[Turn], spec_dirs: dict[str, Path]) -> tuple[list[Turn], list[dict], dict[str, str]]:
    folded_dirs: dict[str, list[str]] = {}
    for spec_id in spec_dirs:
        folded_dirs.setdefault(spec_id.casefold(), []).append(spec_id)

    aliases: dict[str, str] = {}
    blindness: list[dict] = []
    for turn in turns:
        matches = folded_dirs.get(turn.spec_id.casefold(), [])
        if len(matches) == 1:
            canonical = matches[0]
            aliases[turn.spec_id] = turn.spec_id
            if turn.spec_id != canonical:
                blindness.append(
                    _blindness(
                        "ambiguous-spec-id",
                        f"attempt spec_id {turn.spec_id!r} collides case-insensitively with spec directory {canonical!r}",
                        [canonical, turn.spec_id],
                    )
                )
        elif len(matches) > 1:
            affected = sorted(set(matches + [turn.spec_id]))
            blindness.append(
                _blindness(
                    "ambiguous-spec-id",
                    f"case-folded spec_id {turn.spec_id!r} matches multiple spec directories",
                    affected,
                )
            )
        else:
            aliases[turn.spec_id] = turn.spec_id
            blindness.append(
                _blindness(
                    "unmatched-spec-id",
                    f"attempt spec_id {turn.spec_id!r} has no spec directory",
                    [turn.spec_id],
                )
            )
    for folded, names in folded_dirs.items():
        if len(names) > 1:
            blindness.append(
                _blindness(
                    "ambiguous-spec-id",
                    f"spec directories collide after case-folding: {', '.join(sorted(names))}",
                    names,
                )
            )
    return turns, _dedupe_blindness(blindness), aliases


def _applies_to_spec(item: dict, spec_id: str) -> bool:
    specs = item.get("specs", ["*"])
    return "*" in specs or spec_id in specs


def _spec_blindness(items: list[dict], spec_id: str) -> list[dict]:
    return [
        {"class": item["class"], "detail": item["detail"]}
        for item in items
        if _applies_to_spec(item, spec_id)
    ]


def scan_repo(root, *, queue_override=None, check_chain=True) -> dict:
    root = _resolve_path(Path(root), strict=False)
    queue_roots, source, n_candidates, queue_blindness = _resolve_queue_roots_audit(root, queue_override)
    turns, diagnostics, repo_blindness = _load_turns_from_roots(queue_roots)
    repo_blindness.extend(queue_blindness)
    spec_dirs, spec_dir_blindness = _spec_dirs(root)
    repo_blindness.extend(spec_dir_blindness)
    turns, identity_blindness, aliases = _canonicalize_turn_specs(turns, spec_dirs)
    for item in repo_blindness:
        if item.get("specs") != ["*"]:
            item["specs"] = sorted({aliases.get(spec_id, spec_id) for spec_id in item["specs"]})
    repo_blindness.extend(identity_blindness)
    repo_blindness = _dedupe_blindness(repo_blindness)
    spec_ids = set(spec_dirs) | {t.spec_id for t in turns}
    acc_by_spec: dict[str, list[Turn]] = {spec_id: [] for spec_id in spec_ids}
    for turn in turns:
        if turn.decision == "phase-complete" and turn.phase in {"implement", "verify"}:
            acc_by_spec.setdefault(turn.spec_id, []).append(turn)

    specs = []
    for spec_id in sorted(spec_ids):
        claim, claim_blindness = _spec_claim(spec_dirs.get(spec_id))
        for item in claim_blindness:
            item["specs"] = [spec_id]
        repo_blindness.extend(claim_blindness)
        row = stamp_spec(spec_id, acc_by_spec.get(spec_id, []), claim)
        if check_chain:
            chain, chain_findings, chain_blindness = _chain_for_spec(
                spec_dirs.get(spec_id), acc_by_spec.get(spec_id, [])
            )
            row["chain"] = chain
            row["findings"].extend(chain_findings)
            for item in chain_blindness:
                item["specs"] = [spec_id]
            repo_blindness.extend(chain_blindness)
        repo_blindness = _dedupe_blindness(repo_blindness)
        row["blindness"] = _spec_blindness(repo_blindness, spec_id)
        # Hard invariant: blindness can never coexist with host-verified.
        # Known-bad still beats unknowable and retains self-reported.
        if row["blindness"] and row["verification"] == "host-verified":
            row["verification"] = "unknown"
        specs.append(row)

    repo_blindness = _dedupe_blindness(repo_blindness)

    return {
        "root": str(root),
        "queue_root": str(queue_roots[0]) if queue_roots else None,
        "queue_roots": [str(queue_root) for queue_root in queue_roots],
        "queue_root_source": source,
        "queue_candidates": n_candidates,
        "coverage": coverage(turns),
        "specs": specs,
        "diagnostics": diagnostics,
        "blindness": repo_blindness,
    }


def _pct(stats: dict) -> str:
    return "-" if stats["claimed"] == 0 else f"{100 * stats['adjudicated'] // stats['claimed']}%"


def _findings(spec: dict) -> str:
    classes = [f["class"] for f in spec["findings"]]
    return ",".join(classes) if classes else "-"


def _diagnostics_line(diagnostics: dict) -> str:
    parts = []
    unreadable = diagnostics.get("unreadable_attempts", 0)
    without = diagnostics.get("attempts_without_spec_id", 0)
    if unreadable:
        noun = "record" if unreadable == 1 else "records"
        parts.append(f"{unreadable} unreadable attempt {noun}")
    if without:
        noun = "attempt" if without == 1 else "attempts"
        parts.append(f"{without} {noun} without spec_id")
    return "diagnostics: " + ", ".join(parts) if parts else ""


def render_human(report) -> str:
    root = report["root"]
    if (
        not report["specs"]
        and not report.get("queue_roots")
        and not report.get("blindness")
        and all(sum(v.values()) == 0 for v in report["coverage"].values())
    ):
        return f"No specs and no queue history under {root}."

    lines: list[str] = []
    queue_roots = report.get("queue_roots", [])
    if not queue_roots:
        lines.append("QUEUE  (none found under .builder/ — no dispatch history)")
    else:
        lines.append(
            f"QUEUE  {', '.join(queue_roots)}  "
            f"(source: {report['queue_root_source']}, {report.get('queue_candidates', 0)} candidates)"
        )
    lines.append("")
    lines.append("GATE COVERAGE  (claim-turns: the agent declared a gated phase complete)")
    lines.append("  gate             adjudicated  claimed  coverage  unknown  n/a")
    for gate in GATE_NAMES:
        stats = report["coverage"][gate]
        lines.append(
            f"  {gate.ljust(15)} {str(stats['adjudicated']).rjust(12)}"
            f" {str(stats['claimed']).rjust(8)} { _pct(stats).rjust(8)}"
            f" {str(stats['unknown']).rjust(8)} {str(stats['not_applicable']).rjust(5)}"
        )
    lines.append("  unknown = no gate record on the attempt (pre-evidence dispatcher or non-finalized turn);")
    lines.append("  excluded from coverage, never counted as covered.")
    lines.append("")

    specs = report["specs"]
    lines.append("SPECS")
    name_w = max([len(s["spec"]) for s in specs] + [4])
    claim_w = max([len(s["claim"]) for s in specs] + [5])
    ver_w = max([len(s["verification"] or "-") for s in specs] + [12])
    lines.append(
        f"  {'spec'.ljust(name_w)}  {'claim'.ljust(claim_w)}  "
        f"{'verification'.ljust(ver_w)}  turns  unk  loops  findings"
    )
    for spec in specs:
        lines.append(
            f"  {spec['spec'].ljust(name_w)}  {spec['claim'].ljust(claim_w)}  "
            f"{(spec['verification'] or '-').ljust(ver_w)}  "
            f"{str(spec['accepted_turns']).rjust(5)}  {str(spec['unknown_turns']).rjust(3)}  "
            f"{str(spec['rework_loops']).rjust(5)}  {_findings(spec)}"
        )

    if any(spec.get("chain", {}).get("checked") for spec in specs):
        lines.append("")
        lines.append("CHAIN  (gate-evidence hash-chain consistency)")
        for spec in specs:
            chain = spec.get("chain", {"checked": False})
            if not chain.get("checked"):
                continue
            violations = chain.get("violations", [])
            if chain.get("checked") == "partial":
                line = "cross-links checked; hash chain NOT verified (requires PyYAML)"
                if violations:
                    line += f"; VIOLATIONS ({len(violations)}): {'; '.join(violations)}"
                lines.append(f"  {spec['spec'].ljust(name_w)}  {line}")
            elif violations:
                detail = "; ".join(violations)
                lines.append(f"  {spec['spec'].ljust(name_w)}  VIOLATIONS ({len(violations)}): {detail}")
            else:
                lines.append(f"  {spec['spec'].ljust(name_w)}  intact ({chain.get('bundles', 0)} bundles)")
        if any(spec.get("chain", {}).get("checked") is True for spec in specs):
            lines.append(CHAIN_NOTE)

    blindness = report.get("blindness", [])
    if blindness:
        lines.append("")
        lines.append("BLINDNESS")
        for item in blindness:
            affected = item.get("specs", ["*"])
            scope = "all specs" if "*" in affected else ", ".join(affected)
            lines.append(f"  {item['class']:<27} {item['detail']} (affects: {scope})")

    all_findings = [(spec, finding) for spec in specs for finding in spec["findings"]]
    if all_findings:
        lines.append("")
        lines.append("FINDINGS")
        for spec, finding in all_findings:
            klass = finding["class"]
            if klass == "self-certified":
                lines.append(
                    f"  {klass:<17} {spec['spec']}  {finding['attempt_id']} "
                    f"phase={finding['phase']} {finding['detail']}"
                )
                lines.append("                    accepted phase-complete without the host running a single test")
            else:
                lines.append(
                    f"  {klass:<17} {spec['spec']}  {finding['attempt_id']} "
                    f"phase={finding['phase']} {finding['detail']}"
                )

    diag_line = _diagnostics_line(report["diagnostics"])
    if diag_line:
        lines.append("")
        lines.append(diag_line)

    counts = {"host-verified": 0, "self-reported": 0, "unknown": 0, None: 0}
    for spec in specs:
        counts[spec["verification"]] = counts.get(spec["verification"], 0) + 1
    hv = report["coverage"]["host_verify"]
    lines.append("")
    lines.append(
        f"{len(specs)} specs: {counts.get('host-verified', 0)} host-verified, "
        f"{counts.get('self-reported', 0)} self-reported, {counts.get('unknown', 0)} unknown, "
        f"{counts.get(None, 0)} in-flight"
    )
    lines.append(
        f"host_verify coverage {hv['adjudicated']}/{hv['claimed']} claim-turns "
        f"({_pct(hv)}) — {hv['unknown']} turns unknown (no gate record)"
    )
    return "\n".join(lines)


def _has_tripping_findings(report: dict) -> bool:
    return any(
        finding["class"] in CHECK_FINDINGS
        for spec in report.get("specs", [])
        for finding in spec.get("findings", [])
    )


def _repo_children(projects_root: Path) -> list[Path]:
    root_probe = _probe_path(projects_root, "directory", allow_symlink=True)
    if root_probe.state == "missing":
        return []
    if root_probe.state == "blind":
        raise OperationalError(root_probe.detail)
    repos = []
    for child in _dir_entries(Path(root_probe.value)):
        if child.name.startswith("."):
            continue
        child_probe = _probe_path(child, "directory", allow_symlink=True)
        if child_probe.state != "ready":
            continue
        builder_probe = _probe_path(runtime_dir(Path(child_probe.value)), "directory")
        if builder_probe.state == "blind":
            raise OperationalError(builder_probe.detail)
        if builder_probe.state == "ready":
            repos.append(Path(child_probe.value))
    return repos


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Builder gate coverage.",
        epilog=(
            "host-verified = on every host-accepted implement/verify turn of this spec, the host itself ran the verify\n"
            "commands and saw exit 0 (and, on implement turns, confirmed a real source change). It is an\n"
            "**observational** stamp from host-recorded gate outcomes; it does not encode the warn/enforce posture\n"
            "(J1), and it says nothing about turns that predate gate evidence (those force `unknown`)."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root", default=".", help="Project root containing an active runtime directory (default: .)")
    group.add_argument("--all", help="Scan every immediate child repo under DIR")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    parser.add_argument("--check", action="store_true", help="Exit 1 on self-certified, host-contradicted, or chain-violation findings")
    parser.add_argument(
        "--verify-chain", action=argparse.BooleanOptionalAction, default=True,
        help="Verify gate-evidence hash-chain consistency (default: on; --no-verify-chain to opt out)",
    )
    parser.add_argument("--queue-root", help="Explicit queue root override (single-repo only)")
    args = parser.parse_args(argv)

    if args.all and args.queue_root:
        parser.error("--all cannot be used with --queue-root")

    if args.all:
        try:
            projects_root = _resolve_path(Path(args.all), strict=False)
        except OperationalError as exc:
            print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
            return 2
        try:
            repos = _repo_children(projects_root)
        except OperationalError as exc:
            print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
            return 2
        projects_probe = _probe_path(projects_root, "directory", allow_symlink=True)
        if projects_probe.state != "ready" or not repos:
            print(f"No runtime-directory children under {str(projects_root)!r}. Nothing to audit.", file=sys.stderr)
            return 2
        try:
            reports = [scan_repo(repo, check_chain=args.verify_chain) for repo in repos]
        except OperationalError as exc:
            print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"projects_root": str(projects_root), "repos": reports}, indent=2))
        else:
            blocks = []
            for repo, report in zip(repos, reports):
                blocks.append(f"== {repo.name} ({repo})\n\n{render_human(report)}")
            print("\n\n".join(blocks))
            counts = {"host-verified": 0, "self-reported": 0, "unknown": 0, None: 0}
            for report in reports:
                for spec in report["specs"]:
                    counts[spec["verification"]] = counts.get(spec["verification"], 0) + 1
            total_specs = sum(len(report["specs"]) for report in reports)
            print(
                f"ALL: {len(reports)} repos, {total_specs} specs: "
                f"{counts.get('host-verified', 0)} host-verified, "
                f"{counts.get('self-reported', 0)} self-reported, "
                f"{counts.get('unknown', 0)} unknown, {counts.get(None, 0)} in-flight"
            )
        return 1 if args.check and any(_has_tripping_findings(report) for report in reports) else 0

    try:
        root = _resolve_path(Path(args.root), strict=False)
    except OperationalError as exc:
        print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
        return 2
    try:
        builder_probe = _probe_path(runtime_dir(root), "directory")
    except OperationalError as exc:
        print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
        return 2
    if builder_probe.state != "ready":
        print(f"No runtime directory (.builder/) under {str(root)!r}. Nothing to audit.", file=sys.stderr)
        return 2
    try:
        report = scan_repo(root, queue_override=args.queue_root, check_chain=args.verify_chain)
    except OperationalError as exc:
        print(f"gate-coverage: operational error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report))
    return 1 if args.check and _has_tripping_findings(report) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
        raise SystemExit(0)
