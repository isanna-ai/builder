#!/usr/bin/env python3
"""Build and verify the Builder living system model."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _yaml import yaml

from _dispatch_runtime.gate_evidence import CommandResult, classify_failure
from _dispatch_runtime.lane_common import _run_verify_commands_detailed
from _dispatch_runtime.paths import RUNTIME_DIR_NAMES, runtime_dir
from _validators import anchors as anchor_validator
from _validators.tasks import _non_hermetic_reason, _non_probative_reason

SCHEMA = "system-model/v1"
TAIL_CAP = 8192
BANNED_STRINGS = ("forgery-proof", "cryptographically secure", "tamper-proof")


@dataclass(frozen=True)
class RunOutcome:
    command: str
    cwd: str
    result: Any
    failure_class: str | None
    assurance_state: str


class ModelDataError(RuntimeError):
    """Raised when host-built model or ledger data cannot be trusted."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcstamp(now: datetime | None = None) -> str:
    value = now or _utcnow()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelDataError(f"cannot read {path}: {exc}") from exc
    try:
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        return yaml.safe_load(text)
    except Exception as exc:
        raise ModelDataError(f"cannot parse {path}: {exc}") from exc


def _load_spec_mapping(
    path: Path,
    *,
    required: bool = False,
    list_field: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Load one spec artifact without turning malformed input into an empty spec."""
    if not path.exists():
        if not required:
            return {}, []
        return {}, [{"source": path.name, "kind": "missing", "message": f"required artifact {path.name} is missing"}]
    try:
        data = _load_yaml(path)
    except ModelDataError as exc:
        return {}, [{"source": path.name, "kind": "parse_error", "message": str(exc)}]
    if not isinstance(data, dict):
        return {}, [{
            "source": path.name,
            "kind": "type_error",
            "message": f"{path.name} must contain a mapping, got {type(data).__name__}",
        }]
    if list_field is not None and not isinstance(data.get(list_field), list):
        return data, [{
            "source": path.name,
            "kind": "type_error",
            "message": f"{path.name}.{list_field} must be a list",
        }]
    return data, []


def _dump_yaml(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_yaml(data), encoding="utf-8")


def _recordignore(root: Path) -> list[str]:
    patterns: list[str] = []
    for path in (root / ".recordignore", runtime_dir(root) / ".recordignore"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass
    return patterns


def _ignored(spec_id: str, rel_path: str, patterns: list[str]) -> bool:
    if spec_id.startswith("ab-bench-"):
        return True
    return any(fnmatch.fnmatch(spec_id, pat) or fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def _spec_dirs(root: Path) -> dict[str, Path]:
    specs_root = runtime_dir(root) / "specs"
    patterns = _recordignore(root)
    out: dict[str, Path] = {}
    if not specs_root.is_dir():
        return out
    for entry in sorted(specs_root.iterdir(), key=lambda p: p.name):
        if entry.name == "archive" and entry.is_dir():
            for archived in sorted(entry.iterdir(), key=lambda p: p.name):
                rel = f"archive/{archived.name}"
                if archived.is_dir() and not _ignored(archived.name, rel, patterns):
                    out.setdefault(archived.name, archived)
            continue
        if entry.is_dir() and not _ignored(entry.name, entry.name, patterns):
            out.setdefault(entry.name, entry)
    return out


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _task_exempt(task: dict[str, Any]) -> bool:
    if str(task.get("tdd_mode", "")).strip() == "exempt":
        return True
    tdd = task.get("tdd") if isinstance(task.get("tdd"), dict) else {}
    return str(tdd.get("mode", "")).strip() == "exempt"


def _verify_items(task: dict[str, Any]) -> list[Any]:
    raw = task.get("verify")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return []


def _command_and_proves(task: dict[str, Any], item: Any) -> tuple[str, list[str]]:
    if isinstance(item, dict):
        command = str(item.get("command", "")).strip()
        proves = _string_list(item.get("proves")) or _string_list(task.get("proves"))
        return command, proves
    return str(item).strip(), _string_list(task.get("proves"))


def _oracle_type(value: Any) -> str:
    raw = str(value or "unknown").strip() or "unknown"
    if raw in {"automated_test", "bounded_probe", "human_only", "unknown"}:
        return raw
    return "unknown"


def _harvest_task_checks(spec_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    data, findings = _load_spec_mapping(spec_dir / "tasks.yaml", list_field="tasks")
    tasks = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else []
    checks: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            findings.append({
                "source": "tasks.yaml",
                "kind": "type_error",
                "message": f"tasks.yaml.tasks[{index}] must be a mapping",
            })
            continue
        if _task_exempt(task):
            continue
        task_id = str(task.get("id") or f"T{index}").strip() or f"T{index}"
        for verify_index, item in enumerate(_verify_items(task), start=1):
            command, proves = _command_and_proves(task, item)
            if not command:
                continue
            check = {
                "id": task_id,
                "source": "tasks.yaml",
                "command": command,
                "proves": proves,
                "oracle": "automated_test",
                "command_index": verify_index,
            }
            reason = _non_probative_reason(command)
            if reason is not None:
                check["non_probative"] = True
                check["non_probative_reason"] = reason
            checks.append(check)
    return checks, findings


def _ac_id(requirement_id: str, index: int, ac: dict[str, Any]) -> str:
    raw = str(ac.get("id", "")).strip()
    return raw or f"AC-{requirement_id}-{index}"


def _oracle_commands(oracle: dict[str, Any]) -> list[str]:
    """Extract every command from canonical `oracle.expected` command spans."""
    expected = oracle.get("expected")
    values = expected if isinstance(expected, list) else [expected]
    commands: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        spans = [match.strip() for match in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", value) if match.strip()]
        if not spans:
            stripped = value.strip()
            # Tolerate a direct command while keeping prose-only expectations out.
            if "\n" not in stripped and re.match(
                r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:python\d*|pytest|py\.test|sh|bash|npm|pnpm|yarn|cargo|go|make)\b",
                stripped,
            ):
                spans = [stripped]
        for command in spans:
            if command not in commands:
                commands.append(command)
    return commands


def _harvest_ac_oracle_checks(
    spec_dir: Path,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, str]]]:
    data, findings = _load_spec_mapping(spec_dir / "requirements.yaml", list_field="requirements")
    requirements = data.get("requirements") if isinstance(data, dict) and isinstance(data.get("requirements"), list) else []
    checks: list[dict[str, Any]] = []
    found_machine_oracle = False
    for req_index, req in enumerate(requirements, start=1):
        if not isinstance(req, dict):
            findings.append({
                "source": "requirements.yaml",
                "kind": "type_error",
                "message": f"requirements.yaml.requirements[{req_index}] must be a mapping",
            })
            continue
        req_id = str(req.get("id", "")).strip() or "R?"
        acceptance = req.get("acceptance") if isinstance(req.get("acceptance"), list) else []
        if "acceptance" in req and not isinstance(req.get("acceptance"), list):
            findings.append({
                "source": "requirements.yaml",
                "kind": "type_error",
                "message": f"requirements.yaml requirement {req_id} acceptance must be a list",
            })
        for index, ac in enumerate(acceptance, start=1):
            if not isinstance(ac, dict):
                continue
            oracle = ac.get("oracle") if isinstance(ac.get("oracle"), dict) else {}
            if "type" not in oracle:
                continue
            oracle_type = _oracle_type(oracle.get("type"))
            commands = _oracle_commands(oracle)
            if not commands:
                continue
            if oracle_type in {"automated_test", "bounded_probe"}:
                found_machine_oracle = True
            for command_index, command in enumerate(commands, start=1):
                check = {
                    "id": _ac_id(req_id, index, ac),
                    "source": "requirements.yaml",
                    "command": command,
                    "command_index": command_index,
                    "proves": [],
                    "oracle": oracle_type,
                }
                preconditions = _string_list(oracle.get("preconditions"))
                if preconditions:
                    check["preconditions"] = preconditions
                reason = _non_probative_reason(command)
                if reason is not None:
                    check["non_probative"] = True
                    check["non_probative_reason"] = reason
                checks.append(check)
    return checks, found_machine_oracle, findings


def _harvest_anchors(spec_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    data, findings = _load_spec_mapping(spec_dir / "traceability.yaml", list_field="task_links")
    task_links = data.get("task_links") if isinstance(data, dict) and isinstance(data.get("task_links"), list) else []
    anchors: list[dict[str, str]] = []
    for link in task_links:
        if not isinstance(link, dict):
            continue
        files = link.get("files") if isinstance(link.get("files"), list) else []
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            path = str(file_entry.get("path", "")).strip()
            raw_anchors = file_entry.get("anchors") if isinstance(file_entry.get("anchors"), list) else []
            for anchor in raw_anchors:
                if not isinstance(anchor, dict):
                    continue
                value = str(anchor.get("value", anchor.get("locator", ""))).strip()
                kind = str(anchor.get("kind", "")).strip()
                if path and value and kind:
                    anchors.append({"file": path, "kind": kind, "value": value})
    return anchors, findings


def _repo_name(root: Path) -> str:
    return root.resolve().name


def _stale_absolute_anchor_findings(anchors: list[dict[str, str]]) -> list[dict[str, str]]:
    """Flag anchor `file` fields that are absolute and do not resolve on disk.

    Scoped strictly to the anchor `file` field (the one load-bearing path field
    anchors carry — see `_harvest_anchors`/`_anchor_resolves`). This does NOT
    scan check `command` strings or any other free-text field: those
    legitimately contain historical path mentions (temp paths, shell
    variables, quoted heredoc source) that would produce false positives if
    treated as authoritative paths. This is purely additive/informational and
    never rewrites the spec artifact the anchor came from.
    """
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        path = str(anchor.get("file", "")).strip()
        if not path or path in seen or not path.startswith("/"):
            continue
        if Path(path).exists():
            continue
        seen.add(path)
        findings.append({
            "source": "traceability.yaml",
            "kind": "stale_absolute_path",
            "path": path,
            "message": (
                f"absolute path {path} does not resolve from repo root; "
                "it probably points at a checkout that has moved or been renamed"
            ),
        })
    return findings


def build_model(root: Path, out: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    out = Path(out) if out is not None else runtime_dir(root) / "model"
    capabilities: list[dict[str, Any]] = []
    collection_findings: list[dict[str, str]] = []
    for spec_id, spec_dir in _spec_dirs(root).items():
        spec_yaml, spec_findings = _load_spec_mapping(spec_dir / "spec.yaml", required=True)
        claim = str(spec_yaml.get("status", "unknown")).strip() if isinstance(spec_yaml, dict) else "unknown"
        ac_checks, has_ac_oracle, ac_findings = _harvest_ac_oracle_checks(spec_dir)
        task_checks, task_findings = _harvest_task_checks(spec_dir)
        anchors, anchor_findings = _harvest_anchors(spec_dir)
        stale_path_findings = _stale_absolute_anchor_findings(anchors)
        findings = spec_findings + ac_findings + task_findings + anchor_findings + stale_path_findings
        if has_ac_oracle:
            checks = ac_checks + task_checks
            granularity = "ac_oracle"
        elif task_checks:
            # Empty or non-machine AC metadata must not shadow executable task checks.
            checks = task_checks
            granularity = "task_verify"
        else:
            checks = ac_checks
            granularity = "ac_oracle" if ac_checks else "none"
        cap_findings = [{"capability": f"cap:{spec_id}", **finding} for finding in findings]
        collection_findings.extend(cap_findings)
        capabilities.append({
            "key": f"cap:{spec_id}",
            "spec": spec_id,
            "claim": claim or "unknown",
            "archived": any(f"{name}/specs/archive/" in spec_dir.as_posix() for name in RUNTIME_DIR_NAMES) or claim == "archived",
            "granularity": granularity,
            "checks": checks,
            "anchors": anchors,
            "collection_findings": cap_findings,
        })
    model = {
        "schema": SCHEMA,
        "built_at": _utcstamp(now),
        "repo": _repo_name(root),
        "capabilities": capabilities,
        "collection_findings": collection_findings,
    }
    _write_yaml(out / "system-model.yaml", model)
    return model


def _load_model(root: Path, out: Path | None = None) -> dict[str, Any]:
    model_path = (Path(out) if out is not None else runtime_dir(Path(root)) / "model") / "system-model.yaml"
    if not model_path.is_file():
        raise ModelDataError(f"system model missing at {model_path}; run `isanna model build --root {root}` first")
    data = _load_yaml(model_path)
    if not isinstance(data, dict):
        raise ModelDataError(f"invalid system model {model_path}: top level must be a mapping")
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")
    if not isinstance(data.get("repo"), str) or not str(data.get("repo")).strip():
        errors.append("repo must be a non-empty string")
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list):
        errors.append("capabilities must be a list")
        capabilities = []
    for index, cap in enumerate(capabilities):
        if not isinstance(cap, dict):
            errors.append(f"capabilities[{index}] must be a mapping")
            continue
        if not isinstance(cap.get("key"), str) or not cap.get("key"):
            errors.append(f"capabilities[{index}].key must be a non-empty string")
        for field in ("checks", "anchors"):
            if not isinstance(cap.get(field), list):
                errors.append(f"capabilities[{index}].{field} must be a list")
        checks = cap.get("checks") if isinstance(cap.get("checks"), list) else []
        for check_index, check in enumerate(checks):
            if not isinstance(check, dict):
                errors.append(f"capabilities[{index}].checks[{check_index}] must be a mapping")
                continue
            for field in ("id", "source", "command", "oracle"):
                if not isinstance(check.get(field), str):
                    errors.append(f"capabilities[{index}].checks[{check_index}].{field} must be a string")
        anchors = cap.get("anchors") if isinstance(cap.get("anchors"), list) else []
        for anchor_index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                errors.append(f"capabilities[{index}].anchors[{anchor_index}] must be a mapping")
    if errors:
        raise ModelDataError(f"invalid system model {model_path}: " + "; ".join(errors))
    return data


def _git_sha(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"
    except Exception:
        return "unknown"


def _result_tail(result: Any) -> str:
    text = f"{getattr(result, 'stdout_tail', '')}\n{getattr(result, 'stderr_tail', '')}".strip()
    return text[-TAIL_CAP:]


def _exit_code(result: Any) -> int | None:
    return getattr(result, "exit_code", None)


def _ok(result: Any) -> bool:
    return bool(getattr(result, "ok", False))


def _run_one(command: str, cwd: str, runner: Any | None) -> Any:
    if runner is not None:
        value = runner([command], cwd)
        if isinstance(value, list):
            return value[0]
        return value
    return _run_verify_commands_detailed([command], cwd, capture=True)[0]


def _check_ref(cap: dict[str, Any], check: dict[str, Any]) -> str:
    suffix = int(check.get("command_index", 1) or 1)
    base = f"{cap.get('key')}/{check.get('id')}"
    return base if suffix == 1 else f"{base}#{suffix}"


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def _unsafe_reason(command: str, root: Path) -> str | None:
    """Return why an agent-authored historical command must never be executed."""
    patterns = (
        (r"(?:^|[;&|]\s*)rm\s+(?:[^;&|]*\s)?-(?:[A-Za-z]*r[A-Za-z]*f|[A-Za-z]*f[A-Za-z]*r)\b", "rm -rf"),
        (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
        (r"\bgit\s+clean\s+[^;&|\n]*-[A-Za-z]*[fdx][A-Za-z]*\b", "git clean destructive flags"),
        (r"\bgit\s+checkout\s+[^;&|\n]*-f\b", "git checkout -f"),
        (r"(?:^|[;&|]\s*)mkfs(?:\.[A-Za-z0-9_-]+)?\b", "mkfs"),
        (r"(?:^|[;&|]\s*)dd\s+[^;&|\n]*\bif\s*=", "dd if="),
        (r"(?:^|[;&|]\s*)sudo(?:\s|$)", "sudo"),
        (r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:env\s+)?(?:sh|bash)\b", "download piped to a shell"),
    )
    for pattern, label in patterns:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return f"unsafe command matches destructive denylist: {label}"

    tokens = _shell_tokens(command)
    separators = {";", "&&", "||", "|", "&"}
    for index, token in enumerate(tokens):
        lowered = token.lower()
        executable = lowered.rsplit("/", 1)[-1]
        if lowered == "sudo":
            return "unsafe command matches destructive denylist: sudo"
        if executable == "find":
            args = []
            for arg in tokens[index + 1:]:
                if arg in separators:
                    break
                args.append(arg.lower())
            if "-delete" in args:
                return "unsafe command matches destructive denylist: find -delete"
            for action in ("-exec", "-execdir"):
                if action in args:
                    invoked = args[args.index(action) + 1:]
                    if invoked and invoked[0].rsplit("/", 1)[-1] == "rm":
                        return f"unsafe command matches destructive denylist: find {action} rm"
        if executable == "xargs":
            args = []
            for arg in tokens[index + 1:]:
                if arg in separators:
                    break
                args.append(arg.lower())
            if any(arg.rsplit("/", 1)[-1] == "rm" for arg in args):
                return "unsafe command matches destructive denylist: xargs rm"
        if executable == "truncate":
            args = []
            for arg in tokens[index + 1:]:
                if arg in separators:
                    break
                args.append(arg.lower())
            if "-s0" in args or any(arg == "-s" and args[pos + 1:pos + 2] == ["0"]
                                    for pos, arg in enumerate(args)):
                return "unsafe command matches destructive denylist: truncate -s 0"
        if executable == "rm":
            args: list[str] = []
            for arg in tokens[index + 1:]:
                if arg in separators:
                    break
                args.append(arg.lower())
            short_flags = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
            recursive = "r" in short_flags or "--recursive" in args
            force = "f" in short_flags or "--force" in args
            if recursive and force:
                return "unsafe command matches destructive denylist: rm -rf"
        if executable.startswith("mkfs"):
            return "unsafe command matches destructive denylist: mkfs"
        if executable == "dd":
            args = []
            for arg in tokens[index + 1:]:
                if arg in separators:
                    break
                args.append(arg.lower())
            if any(arg.startswith("if=") for arg in args):
                return "unsafe command matches destructive denylist: dd if="
    for index, token in enumerate(tokens[:-1]):
        if token not in {">", ">>"}:
            continue
        segment_start = max(
            (pos + 1 for pos in range(index) if tokens[pos] in separators),
            default=0,
        )
        if token == ">" and all(part == ":" or part.isdigit() for part in tokens[segment_start:index]):
            return "unsafe command matches destructive denylist: output redirect clobber"
        target = tokens[index + 1]
        if target.startswith("&") or target == "/dev/null":
            continue
        if target.startswith(("$", "~")):
            return f"unsafe output redirect may leave project: {target}"
        path = Path(target)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            return f"unsafe output redirect leaves project: {target}"
    return None


_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "npx"}
_DIRECT_JS_RUNNERS = {"vitest", "jest", "mocha"}
# An explicit config flag lets a recorded command point the runner at arbitrary executable code.
_RUNNER_CONFIG_FLAGS = {"--config", "-c", "--project", "--projects"}


def _points_at_foreign_config(args: list[str]) -> bool:
    """True if the command explicitly selects a config file rather than the project's own."""
    return any(a in _RUNNER_CONFIG_FLAGS or a.startswith(("--config=", "--project=")) for a in args)


def _binary_after_package_manager(manager: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Resolve `pnpm exec <bin> ...` / `npx <bin> ...` to (bin, rest); None if it is script indirection.

    Binary RESOLUTION is transparent -- it runs the named executable. Script indirection (`pnpm test`)
    runs whatever the CURRENT package.json maps that name to, which is the escape hatch we refuse.
    """
    rest = list(args)
    while rest and rest[0].startswith("-"):
        # `pnpm --filter p exec ...` still ends in a binary, but a flag taking a remote package
        # (`--package`) or any dlx-style fetch must never be accepted.
        if rest[0] in {"--package", "-p"} or rest[0].startswith("--package="):
            return None
        rest.pop(0)
        if rest and not rest[0].startswith("-") and rest[0] not in {"exec", "dlx", "run"}:
            # consumed a flag VALUE (e.g. `--filter @scope/pkg`)
            rest.pop(0)
    if not rest:
        return None
    if rest[0] == "dlx":
        return None  # fetches a remote package
    if manager == "npx":
        head = rest[0]
        return (head, rest[1:]) if head not in {"-p", "--package"} else None
    if rest[0] != "exec" or len(rest) < 2:
        return None  # `pnpm test` / `pnpm run x` / `pnpm build` -- project-authored script
    return (rest[1], rest[2:])


def _execution_allowlist_reason(command: str) -> str | None:
    """Refuse commands whose leading executable is not a known test runner."""
    tokens = _shell_tokens(command)
    if "\n" in command or any(re.fullmatch(r"[|&;<>]+", token) for token in tokens):
        return "not a recognized test runner (execution is allowlist-gated)"
    if any("$(" in token or "`" in token or token.startswith(("<(", ">(")) for token in tokens):
        return "not a recognized test runner (execution is allowlist-gated)"
    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
    while tokens and assignment.fullmatch(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return "not a recognized test runner (execution is allowlist-gated)"

    executable = tokens[0]
    args = tokens[1:]
    # ONLY runners that DISCOVER-AND-RUN test files. Deliberately excluded: make / npm / pnpm /
    # yarn / gradle / mvn / tox / setup.py -- each executes a PROJECT-AUTHORED build or package
    # script, so a historical `npm test` re-run against a live tree runs whatever the current
    # package.json says, which is an arbitrary-code escape hatch masquerading as a test runner
    # (adversarial review, R3). That exclusion STANDS: `pnpm test` is still refused below.
    #
    # jest/vitest/mocha were called out as "safe in principle but omitted for now -- add them once
    # confirmed they cannot be pointed at a shelling config". That condition is now discharged by
    # _RUNNER_CONFIG_FLAGS: an explicit --config/-c/--project lets a recorded command aim the runner
    # at an arbitrary config file, and a config file IS executable code (vitest.config.ts, jest.config.js).
    # Without such a flag the runner loads the project's own config from the project root, which is
    # exactly the trust model pytest+conftest.py already has here.
    #
    # When in doubt, refuse: an unrecognized runner loses coverage, never mis-executes.
    if executable in _PACKAGE_MANAGERS:
        # `pnpm exec vitest ...` / `npx vitest ...` RESOLVE A BINARY -- that is not the package-script
        # indirection the exclusion is about. `pnpm test`, `pnpm run x`, `pnpm --filter p build` are,
        # and stay refused. `dlx`/`--package` fetch REMOTE code and are never acceptable.
        inner = _binary_after_package_manager(executable, args)
        if inner is None:
            return "not a recognized test runner (execution is allowlist-gated)"
        executable, args = inner

    recognized = executable in {"pytest", "py.test", "ctest", "rspec", "phpunit", "bats"} or (
        executable in _DIRECT_JS_RUNNERS and not _points_at_foreign_config(args)
    )
    if executable in {"python", "python3"}:
        recognized = len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "unittest"}
    elif executable in {"go", "cargo", "deno", "dotnet"}:
        recognized = bool(args) and args[0] == "test"
    elif executable == "node":
        # Node's BUILT-IN runner discovers and runs test files; `node scripts/whatever.mjs` does not.
        recognized = bool(args) and args[0] == "--test"

    if recognized:
        return None
    return "not a recognized test runner (execution is allowlist-gated)"


def _claims_to_run_tests(command: str) -> bool:
    return bool(re.search(
        r"(?:^|[\s;&|/])(?:pytest|py\.test|unittest|jest|vitest|mocha|nose2?|rspec)(?:\s|$)|"
        r"\b(?:cargo|go|dotnet)\s+test\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|"
        r"\b(?:mvn|gradle|gradlew)\b[^;&|]*\btest\b",
        command,
        flags=re.IGNORECASE,
    ))


def _vacuous_reason(command: str, result: Any) -> str | None:
    output = f"{getattr(result, 'stdout_tail', '')}\n{getattr(result, 'stderr_tail', '')}".strip()
    lowered = output.lower()
    if re.search(r"\bno tests? ran\b|\bcollected\s+0\s+items?\b", lowered):
        return "test runner collected no tests"
    counts: dict[str, int] = {}
    for count, label in re.findall(
        r"\b(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b", lowered
    ):
        counts[label.rstrip("s")] = counts.get(label.rstrip("s"), 0) + int(count)
    executed = sum(counts.get(label, 0) for label in ("passed", "failed", "error", "xfailed", "xpassed"))
    if counts and executed == 0 and (counts.get("skipped", 0) or counts.get("deselected", 0) or "0 passed" in lowered):
        return "all collected tests were skipped or deselected"
    if _exit_code(result) == 0 and not output and _claims_to_run_tests(command):
        return "test command exited 0 with empty output"
    return None


def _classify_result(command: str, result: Any) -> str:
    if getattr(result, "timed_out", False):
        return "timeout"
    if getattr(result, "spawn_error", "") or _exit_code(result) is None:
        return "infrastructure"
    text = f"{getattr(result, 'stderr_tail', '')}\n{getattr(result, 'stdout_tail', '')}"
    if re.search(
        r"connection (?:refused|reset|timed out)|network is unreachable|temporary failure in name resolution|"
        r"could not resolve host|service unavailable|database .* unavailable",
        text,
        flags=re.IGNORECASE,
    ):
        return "infrastructure"
    if re.search(r"(?:^|[\s/])(?:pytest|py\.test)(?:\s|$)", command) and _exit_code(result) in {4, 5}:
        return "collection_error"
    if _claims_to_run_tests(command) and re.search(
        r"error(?:s)? during collection|collected\s+\d+\s+items?\s*/\s*\d+\s+errors?|"
        r"ImportError|ModuleNotFoundError|SyntaxError|Cannot find module|"
        r"file or directory not found:|not found:\s+[^\n]*test",
        text,
        flags=re.IGNORECASE,
    ):
        return "collection_error"
    return classify_failure(result)


def _static_check_state(check: dict[str, Any], root: Path) -> tuple[str | None, str | None]:
    command = str(check.get("command", "")).strip()
    if not command:
        return "claimed", "no executable command"
    unsafe = _unsafe_reason(command, root)
    if unsafe:
        return "unverifiable:unsafe", unsafe
    nonhermetic = _non_hermetic_reason(command)
    if nonhermetic:
        return "unverifiable:non_hermetic", nonhermetic
    if check.get("oracle") not in {"automated_test", "bounded_probe"}:
        return "unverifiable:oracle", f"oracle type {check.get('oracle', 'unknown')} is not machine-scored"
    if check.get("non_probative"):
        return "non_probative", str(check.get("non_probative_reason") or "command cannot prove a result")
    unrecognized = _execution_allowlist_reason(command)
    if unrecognized:
        return "unverifiable:unsafe", unrecognized
    return None, None


def _verification_plan(
    model: dict[str, Any], root: Path, capability_key: str | None
) -> tuple[dict[tuple[str, str], list[str]], dict[str, dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    checks_by_ref: dict[str, dict[str, Any]] = {}
    ref_states: dict[str, str] = {}
    plan: list[dict[str, Any]] = []
    cwd = str(root.resolve())
    for cap in model.get("capabilities", []):
        if capability_key and cap.get("key") != capability_key:
            continue
        for check in cap.get("checks", []):
            ref = _check_ref(cap, check)
            checks_by_ref[ref] = check
            state, reason = _static_check_state(check, root)
            command = str(check.get("command", "")).strip()
            if state:
                ref_states[ref] = state
                plan.append({"action": "skip", "command": command or "<no command>", "reason": reason, "proves": [ref]})
                continue
            grouped.setdefault((command, cwd), []).append(ref)
    for (command, cwd), refs in sorted(grouped.items()):
        checks = [checks_by_ref[ref] for ref in refs]
        preconditions = sorted({item for check in checks for item in _string_list(check.get("preconditions"))})
        plan.append({
            "action": "would_run",
            "command": command,
            "cwd": cwd,
            "preconditions": preconditions,
            "proves": sorted(refs),
        })
    return grouped, checks_by_ref, ref_states, plan


def _ledger_path(root: Path, out: Path | None, now: datetime | None, run_id: str) -> Path:
    date = (now or _utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%d")
    base = Path(out) if out is not None else runtime_dir(Path(root)) / "model"
    return base / "verification" / date / f"{run_id}.yaml"


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
        raise ModelDataError(f"invalid verification ledger {path}: expected a list of entries")
    return data


def _write_run_ledger(
    root: Path, out: Path | None, entries: list[dict[str, Any]], now: datetime | None, run_id: str
) -> Path:
    path = _ledger_path(root, out, now, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(_dump_yaml(entries))
    except FileExistsError as exc:
        raise ModelDataError(f"refusing to overwrite immutable verification ledger {path}") from exc
    return path


def verify_model(
    root: Path,
    out: Path | None = None,
    *,
    capability_key: str | None = None,
    runner: Any | None = None,
    now: datetime | None = None,
    execute: bool = False,
) -> tuple[dict[str, Any], str]:
    root = Path(root).resolve()
    model = _load_model(root, out)
    grouped, checks_by_ref, ref_states, plan = _verification_plan(model, root, capability_key)
    sha = _git_sha(root)
    stamp = now or _utcnow()
    run_id = (
        f"sweep-{stamp.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"
        f"-{os.getpid()}-{time.monotonic_ns()}"
    )
    outcomes: dict[tuple[str, str], RunOutcome] = {}
    ledger_entries: list[dict[str, Any]] = []
    if not execute:
        report = _assurance_report(
            model, outcomes, root, capability_key=capability_key, ref_states=ref_states, plan=plan, mode="dry-run"
        )
        return report, _format_verify_report(report)

    runnable: dict[tuple[str, str], list[str]] = {}
    for (command, cwd), refs in sorted(grouped.items()):
        ready_refs: list[str] = []
        for ref in refs:
            check = checks_by_ref[ref]
            unmet = False
            for precondition in _string_list(check.get("preconditions")):
                refusal = _unsafe_reason(precondition, root)
                if refusal is None:
                    refusal = _non_hermetic_reason(precondition)
                if refusal is None:
                    refusal = _execution_allowlist_reason(precondition)
                if refusal:
                    ref_states[ref] = "unverifiable:precondition"
                    unmet = True
                    break
                pre_result = _run_one(precondition, cwd, runner)
                if not _ok(pre_result):
                    ref_states[ref] = "unverifiable:precondition"
                    unmet = True
                    break
            if not unmet:
                ready_refs.append(ref)
        if ready_refs:
            runnable[(command, cwd)] = ready_refs

    for (command, cwd), proves in sorted(runnable.items()):
        result = _run_one(command, cwd, runner)
        if getattr(result, "timed_out", False):
            retry = _run_one(command, cwd, runner)
            if not getattr(retry, "timed_out", False):
                result = retry
        vacuous = _vacuous_reason(command, result)
        failure_class = None if _ok(result) or vacuous else _classify_result(command, result)
        if vacuous:
            assurance_state = "vacuous"
        elif _ok(result):
            assurance_state = "proven"
        elif failure_class == "assertion_failure":
            assurance_state = "broken"
        elif failure_class == "collection_error":
            assurance_state = "check_rotted"
        else:
            assurance_state = failure_class or "infrastructure"
        outcomes[(command, cwd)] = RunOutcome(command, cwd, result, failure_class, assurance_state)
        for ref in proves:
            ref_states[ref] = assurance_state
        ledger_entries.append({
            "run": {"id": run_id, "trigger": "manual", "git_sha": sha},
            "command": command,
            "cwd": cwd,
            "exit_code": _exit_code(result),
            "failure_class": failure_class,
            "assurance_state": assurance_state,
            "vacuous_reason": vacuous,
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
            "stdout_tail": _result_tail(result),
            "proves": sorted(proves),
            "source": "host",
        })
    if ledger_entries:
        _write_run_ledger(root, out, ledger_entries, stamp, run_id)
    report = _assurance_report(
        model, outcomes, root, capability_key=capability_key, ref_states=ref_states, plan=plan, mode="execute"
    )
    text = _format_verify_report(report)
    return report, text


def _check_state(cap: dict[str, Any], check: dict[str, Any], ref_states: dict[str, str]) -> str:
    return ref_states.get(_check_ref(cap, check), "stale")


def _capability_state(
    cap: dict[str, Any], ref_states: dict[str, str]
) -> tuple[str, list[tuple[dict[str, Any], str]]]:
    checks = [check for check in cap.get("checks", []) if isinstance(check, dict)]
    if cap.get("collection_findings"):
        states = [(check, _check_state(cap, check, ref_states)) for check in checks]
        if any(check_state == "broken" for _, check_state in states):
            return "broken", states
        return "check_rotted", states
    if not checks:
        return "claimed", []
    states = [(check, _check_state(cap, check, ref_states)) for check in checks]
    order = (
        "broken",
        "check_rotted",
        "infrastructure",
        "timeout",
        "vacuous",
        "non_probative",
        "unverifiable:unsafe",
        "unverifiable:non_hermetic",
        "unverifiable:precondition",
        "unverifiable:oracle",
        "claimed",
        "stale",
        "proven",
    )
    for state in order:
        if any(check_state == state for _, check_state in states):
            return state, states
    return "claimed", states


def _assurance_report(
    model: dict[str, Any],
    outcomes: dict[tuple[str, str], RunOutcome],
    root: Path,
    *,
    capability_key: str | None = None,
    ref_states: dict[str, str] | None = None,
    plan: list[dict[str, Any]] | None = None,
    mode: str = "execute",
) -> dict[str, Any]:
    ref_states = dict(ref_states or {})
    failures_by_command: dict[str, str] = {}
    for outcome in outcomes.values():
        if outcome.failure_class:
            failures_by_command[outcome.command] = outcome.failure_class or "assertion_failure"
    count_states = (
        "proven", "broken", "check_rotted", "stale", "vacuous", "infrastructure", "timeout",
        "unverifiable:unsafe", "unverifiable:non_hermetic", "unverifiable:precondition",
        "unverifiable:oracle", "non_probative", "claimed",
    )
    counts = {state: 0 for state in count_states}
    capabilities: list[dict[str, Any]] = []
    infrastructure: dict[str, dict[str, Any]] = {}
    timeout_checks: list[dict[str, str]] = []
    vacuous_checks: list[dict[str, str]] = []
    rotted_checks: list[dict[str, str]] = []
    for cap in model.get("capabilities", []):
        if not isinstance(cap, dict):
            continue
        if capability_key and cap.get("key") != capability_key:
            continue
        state, check_states = _capability_state(cap, ref_states)
        counts[state] += 1
        broken_checks: list[dict[str, str]] = []
        cap_rotted: list[dict[str, str]] = []
        for check, check_state in check_states:
            command = str(check.get("command", "")).strip()
            failure_class = failures_by_command.get(command)
            if failure_class == "infrastructure":
                cluster = infrastructure.setdefault(command, {"command": command, "capabilities": set()})
                cluster["capabilities"].add(cap.get("key"))
            if check_state == "broken":
                broken_checks.append({
                    "id": str(check.get("id", "")),
                    "command": command,
                    "failure_class": failure_class or "assertion_failure",
                })
            elif check_state == "check_rotted":
                item = {
                    "capability": str(cap.get("key", "")),
                    "id": str(check.get("id", "")),
                    "command": command,
                    "failure_class": "collection_error",
                }
                rotted_checks.append(item)
                cap_rotted.append(item)
            elif check_state == "timeout":
                timeout_checks.append({
                    "capability": str(cap.get("key", "")),
                    "id": str(check.get("id", "")),
                    "command": command,
                })
            elif check_state == "vacuous":
                vacuous_checks.append({
                    "capability": str(cap.get("key", "")),
                    "id": str(check.get("id", "")),
                    "command": command,
                })
        capabilities.append({
            "key": cap.get("key"),
            "spec": cap.get("spec"),
            "granularity": cap.get("granularity"),
            "state": state,
            "broken_checks": broken_checks,
            "rotted_checks": cap_rotted,
            "check_states": [
                {"id": str(check.get("id", "")), "command": str(check.get("command", "")), "state": check_state}
                for check, check_state in check_states
            ],
            "collection_findings": cap.get("collection_findings", []),
        })
    counts["unverifiable"] = counts["infrastructure"] + counts["timeout"] + sum(
        count for state, count in counts.items() if state.startswith("unverifiable:")
    )
    machine_denominator = counts["proven"] + counts["broken"] + counts["stale"]
    infra_list = [
        {"command": item["command"], "capabilities": sorted(item["capabilities"]), "count": len(item["capabilities"])}
        for item in infrastructure.values()
    ]
    return {
        "repo": model.get("repo") or _repo_name(root),
        "git_sha": _git_sha(root),
        "capability_count": len(capabilities),
        "counts": counts,
        "capabilities": capabilities,
        "machine_denominator": machine_denominator,
        "machine_holds": counts["proven"],
        "infrastructure": infra_list,
        "timeouts": timeout_checks,
        "vacuous_checks": vacuous_checks,
        "rotted_checks": rotted_checks,
        "collection_findings": [
            finding
            for cap in capabilities
            for finding in cap.get("collection_findings", [])
        ],
        "mode": mode,
        "plan": plan or [],
        "commands_executed": len(outcomes),
    }


def _short_command(command: str) -> str:
    return command if len(command) <= 96 else command[:93] + "..."


def _format_verify_report(report: dict[str, Any]) -> str:
    lines = [
        f"SYSTEM MODEL  {report['repo']}   built {report['git_sha']}   {report['capability_count']} capabilities",
        "",
    ]
    if report.get("mode") == "dry-run":
        lines.extend([
            "DRY RUN - no commands executed. Pass --execute to run eligible checks.",
            "",
            "PLAN",
        ])
        for item in report.get("plan", []):
            if item.get("action") == "would_run":
                preconditions = item.get("preconditions") or []
                suffix = f" (after {len(preconditions)} precondition(s))" if preconditions else ""
                lines.append(f"  WOULD RUN  {_short_command(str(item.get('command', '')))}{suffix}")
            else:
                lines.append(
                    f"  SKIP       {_short_command(str(item.get('command', '')))} - {item.get('reason', 'ineligible')}"
                )
        lines.append("")
    else:
        refused = sum(1 for item in report.get("plan", []) if item.get("action") == "skip")
        lines.extend([
            f"EXECUTE - ran {report.get('commands_executed', 0)} deduplicated command(s); "
            f"refused {refused} check(s).",
            "",
        ])
    counts = report["counts"]
    for state in (
        "proven", "broken", "check_rotted", "stale", "vacuous", "infrastructure", "timeout",
        "unverifiable:non_hermetic", "unverifiable:unsafe", "unverifiable:precondition",
        "unverifiable:oracle", "non_probative", "claimed",
    ):
        lines.append(f"  {state:<36}{counts[state]}")
    broken = [cap for cap in report["capabilities"] if cap["state"] == "broken"]
    if broken:
        lines.extend(["", "BROKEN - these capabilities no longer work"])
        for cap in broken:
            for check in cap["broken_checks"]:
                lines.append(f"  {cap['key']}   {check['id']}   {check['failure_class']}   {_short_command(check['command'])}")
                lines.append("    last passed: never recorded")
    if report["rotted_checks"] or report["collection_findings"]:
        lines.extend(["", "CHECK ROTTED - the check no longer runs; behaviour is unknown"])
        for check in report["rotted_checks"]:
            lines.append(
                f"  {check['capability']}   {check['id']}   collection_error   {_short_command(check['command'])}"
            )
        for finding in report["collection_findings"]:
            lines.append(
                f"  {finding.get('capability', '?')}   {finding.get('source')}   "
                f"{finding.get('kind')}   {finding.get('message')}"
            )
    if report["infrastructure"]:
        lines.extend(["", "INFRASTRUCTURE - clustered host/environment failures"])
        for item in report["infrastructure"]:
            lines.append(f"  {_short_command(item['command'])}   affects {item['count']} capabilities")
    if report["timeouts"]:
        lines.extend(["", "TIMEOUT - checks remained inconclusive after one retry"])
        for item in report["timeouts"]:
            lines.append(f"  {item['capability']}   {item['id']}   {_short_command(item['command'])}")
    if report["vacuous_checks"]:
        lines.extend(["", "VACUOUS - these checks ran nothing; each is an assurance hole"])
        for item in report["vacuous_checks"]:
            lines.append(f"  {item['capability']}   {item['id']}   {_short_command(item['command'])}")
    extra = []
    if counts["unverifiable"]:
        extra.append(f"+{counts['unverifiable']} unverifiable")
    if counts["claimed"]:
        extra.append(f"{counts['claimed']} claimed-only")
    suffix = f"  ({', '.join(extra)})" if extra else ""
    lines.append("")
    if report["machine_denominator"] == 0:
        lines.append(
            f"  0 capabilities machine-verified — NOTHING was checked "
            f"(+{counts['unverifiable']} unverifiable, {counts['claimed']} claimed-only). "
            "This is a blind spot, not a clean bill of health."
        )
    else:
        lines.append(
            f"  {report['machine_holds']}/{report['machine_denominator']} "
            f"machine-checkable capabilities currently hold.{suffix}"
        )
    return "\n".join(lines) + "\n"


def _latest_ledger_entries(root: Path, out: Path | None) -> list[dict[str, Any]]:
    base = Path(out) if out is not None else runtime_dir(Path(root)) / "model"
    verification = base / "verification"
    if not verification.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(verification.rglob("*.yaml")):
        entries.extend(_read_ledger(path))
    return entries


def _latest_command_status(root: Path, out: Path | None) -> dict[str, bool]:
    statuses: dict[str, bool] = {}
    for entry in _latest_ledger_entries(root, out):
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command", "")).strip()
        if command:
            statuses[command] = entry.get("assurance_state") == "proven"
    return statuses


def _anchor_resolves(root: Path, anchor: dict[str, str]) -> bool:
    path = root / anchor["file"]
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return anchor_validator._matches(text, anchor["kind"], anchor["value"])


def _locator_hits_elsewhere(root: Path, anchor: dict[str, str]) -> list[str]:
    hits: list[str] = []
    skip_dirs = {".git", ".pytest_cache", "__pycache__", *(f"{name}/model" for name in RUNTIME_DIR_NAMES)}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel == skip or rel.startswith(skip + "/") for skip in skip_dirs):
            continue
        if rel == anchor["file"]:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if anchor_validator._matches(text, anchor["kind"], anchor["value"]):
                hits.append(rel)
        except (OSError, re.error):
            continue
    return hits


def _cap_oracle_passing(cap: dict[str, Any], command_status: dict[str, bool]) -> bool:
    commands = [
        str(check.get("command", "")).strip()
        for check in cap.get("checks", [])
        if isinstance(check, dict) and str(check.get("command", "")).strip() and not check.get("non_probative")
    ]
    if not commands:
        return False
    return all(command_status.get(command) is True for command in commands)


def drift_model(root: Path, out: Path | None = None) -> tuple[dict[str, Any], str]:
    root = Path(root).resolve()
    model = _load_model(root, out)
    command_status = _latest_command_status(root, out)
    findings: list[dict[str, Any]] = []
    for cap in model.get("capabilities", []):
        if not isinstance(cap, dict):
            continue
        oracle_passing = _cap_oracle_passing(cap, command_status)
        for anchor in cap.get("anchors", []):
            if not isinstance(anchor, dict):
                continue
            norm = {
                "file": str(anchor.get("file", "")),
                "kind": str(anchor.get("kind", "")),
                "value": str(anchor.get("value", "")),
            }
            if not all(norm.values()):
                continue
            if norm["kind"] not in {"literal_string", "regex_v1", "symbol_v1"}:
                findings.append({
                    "capability": cap.get("key"),
                    "severity": "invalid",
                    "verdict": "INVALID ANCHOR KIND - drift unknown",
                    "anchor": norm,
                })
                continue
            if norm["kind"] == "regex_v1":
                try:
                    re.compile(norm["value"], flags=re.MULTILINE)
                except re.error as exc:
                    findings.append({
                        "capability": cap.get("key"),
                        "severity": "invalid",
                        "verdict": f"INVALID ANCHOR REGEX - drift unknown ({exc})",
                        "anchor": norm,
                    })
                    continue
            try:
                resolves = _anchor_resolves(root, norm)
            except OSError:
                resolves = False
            if resolves and oracle_passing:
                continue
            if resolves and not oracle_passing:
                findings.append({
                    "capability": cap.get("key"),
                    "severity": "medium",
                    "verdict": "regression, structure intact",
                    "anchor": norm,
                })
                continue
            hits = _locator_hits_elsewhere(root, norm)
            if oracle_passing:
                findings.append({
                    "capability": cap.get("key"),
                    "severity": "low",
                    "verdict": "moved" if hits else "rewritten but held",
                    "anchor": norm,
                    "suggestions": hits,
                })
            else:
                findings.append({
                    "capability": cap.get("key"),
                    "severity": "high",
                    "verdict": "SUSPECTED FUNCTIONALITY LOST",
                    "anchor": norm,
                    "resolution": "resolution must be a spec: either a fix, or an explicit retires disposition",
                })
    report = {"repo": model.get("repo") or _repo_name(root), "findings": findings}
    return report, _format_drift_report(report)


def _format_drift_report(report: dict[str, Any]) -> str:
    counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "invalid": 0}
    for finding in report["findings"]:
        counts[finding.get("severity", "low")] = counts.get(finding.get("severity", "low"), 0) + 1
    lines = [
        f"SYSTEM MODEL DRIFT  {report['repo']}   {len(report['findings'])} findings",
        f"  high   {counts.get('high', 0)}",
        f"  medium {counts.get('medium', 0)}",
        f"  low    {counts.get('low', 0)}",
        f"  invalid {counts.get('invalid', 0)}",
    ]
    for finding in report["findings"]:
        lines.append("")
        lines.append(f"{finding['severity'].upper()}  {finding['capability']}  {finding['verdict']}")
        anchor = finding.get("anchor", {})
        lines.append(f"  anchor: {anchor.get('file')} {anchor.get('kind')} {anchor.get('value')}")
        if finding.get("resolution"):
            lines.append(f"  {finding['resolution']}")
    return "\n".join(lines) + "\n"


def stale_model(root: Path) -> tuple[dict[str, Any], str]:
    """Compare the published system model against a fresh in-memory regen.

    Read-only advisory: never writes the published model. The model legitimately
    trails spec advancement between syncs, so this is informational only.
    """
    root = Path(root).resolve()
    published_path = runtime_dir(root) / "model" / "system-model.yaml"
    if not published_path.is_file():
        report = {
            "repo": _repo_name(root),
            "fresh": False,
            "error": f"no published model at {published_path}; run `isanna model build --root {root}` first",
        }
        return report, _format_stale_report(report)
    published = _load_yaml(published_path)
    if not isinstance(published, dict):
        published = {}
    with tempfile.TemporaryDirectory() as tmp:
        fresh = build_model(root, out=Path(tmp))
    fresh_built_at = fresh.pop("built_at", None)
    published_built_at = dict(published).pop("built_at", None)
    published_stripped = {k: v for k, v in published.items() if k != "built_at"}
    if fresh == published_stripped:
        report = {"repo": _repo_name(root), "fresh": True}
        return report, _format_stale_report(report)
    fresh_caps = {
        cap.get("key"): cap for cap in fresh.get("capabilities", []) if isinstance(cap, dict)
    }
    published_caps = {
        cap.get("key"): cap
        for cap in published.get("capabilities", [])
        if isinstance(cap, dict)
    }
    added = sorted(key for key in fresh_caps if key not in published_caps)
    removed = sorted(key for key in published_caps if key not in fresh_caps)
    changed = sorted(
        key
        for key in fresh_caps
        if key in published_caps and fresh_caps[key] != published_caps[key]
    )
    report = {
        "repo": _repo_name(root),
        "fresh": False,
        "added": added,
        "removed": removed,
        "changed": changed,
        "published_built_at": published_built_at,
    }
    return report, _format_stale_report(report)


def _format_stale_report(report: dict[str, Any]) -> str:
    if report.get("error"):
        return f"SYSTEM MODEL STALE  {report['repo']}   error: {report['error']}\n"
    if report.get("fresh"):
        return f"SYSTEM MODEL STALE  {report['repo']}   fresh (published model matches a fresh regen)\n"
    lines = [
        f"SYSTEM MODEL STALE  {report['repo']}   published model is stale vs a fresh regen",
        f"  published built_at: {report.get('published_built_at')}",
        f"  added   {len(report.get('added', []))}",
        f"  removed {len(report.get('removed', []))}",
        f"  changed {len(report.get('changed', []))}",
    ]
    for key in report.get("added", []):
        lines.append(f"  ADDED    {key}")
    for key in report.get("removed", []):
        lines.append(f"  REMOVED  {key}")
    for key in report.get("changed", []):
        lines.append(f"  CHANGED  {key}")
    lines.append("  resolution: run `isanna model build` to republish, or ignore until the next sync")
    return "\n".join(lines) + "\n"


def _assert_no_banned(text: str) -> None:
    lower = text.lower()
    for banned in BANNED_STRINGS:
        if banned in lower:
            raise RuntimeError("banned output string present")


def _cmd_build(args: argparse.Namespace) -> int:
    model = build_model(Path(args.root), Path(args.out) if args.out else None)
    path = (Path(args.out) if args.out else runtime_dir(Path(args.root)) / "model") / "system-model.yaml"
    text = (
        f"built {len(model.get('capabilities', []))} capabilities -> {path}\n"
        f"collection findings: {len(model.get('collection_findings', []))}\n"
    )
    _assert_no_banned(text)
    print(text, end="")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.all:
        roots = [path for path in sorted(Path(args.all).iterdir()) if (runtime_dir(path) / "model" / "system-model.yaml").is_file()]
    else:
        roots = [Path(args.root)]
    reports = []
    texts = []
    for root in roots:
        report, text = verify_model(root, capability_key=args.capability, execute=args.execute)
        reports.append(report)
        texts.append(text)
    if args.json:
        output = json.dumps(reports[0] if len(reports) == 1 else reports, indent=2) + "\n"
    else:
        output = "\n".join(texts)
    _assert_no_banned(output)
    print(output, end="")
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    report, text = drift_model(Path(args.root))
    output = json.dumps(report, indent=2) + "\n" if args.json else text
    _assert_no_banned(output)
    print(output, end="")
    return 0


def _cmd_stale(args: argparse.Namespace) -> int:
    report, text = stale_model(Path(args.root))
    output = json.dumps(report, indent=2) + "\n" if args.json else text
    _assert_no_banned(output)
    print(output, end="")
    if args.check and not report.get("fresh"):
        return 3
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model.py")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--root", default=".")
    build.add_argument("--out", default=None)
    build.set_defaults(func=_cmd_build)
    verify = sub.add_parser("verify")
    verify.add_argument("--root", default=".")
    verify.add_argument("--all", default=None)
    verify.add_argument("--capability", default=None)
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--execute", action="store_true", help="execute eligible checks (default: print plan only)")
    verify.set_defaults(func=_cmd_verify)
    drift = sub.add_parser("drift")
    drift.add_argument("--root", default=".")
    drift.add_argument("--json", action="store_true")
    drift.set_defaults(func=_cmd_drift)
    stale = sub.add_parser("stale")
    stale.add_argument("--root", default=".")
    stale.add_argument("--json", action="store_true")
    stale.add_argument("--check", action="store_true")
    stale.set_defaults(func=_cmd_stale)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ModelDataError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
