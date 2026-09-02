"""Gate evidence bundle data model and hash-chain helpers.

Bundles are written as YAML only when the active YAML implementation proves it can
round-trip hostile multiline strings losslessly.  Otherwise they are written as JSON,
which remains valid YAML and preserves the canonical-JSON hash on every supported host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _yaml import yaml

EVIDENCE_DIRNAME = "gate-evidence"
SCHEMA = "gate-evidence/v1"
TAIL_BYTES = 65536
CMD_TAIL_BYTES = 16384
GIT_EVIDENCE_TIMEOUT = 10
CAPTURE_FILE_BYTES = 4 * 1024 * 1024

_DISPATCHER_VERSION: str | None = None


_OFF_VALUES = ("off", "0", "false", "no")
_ON_VALUES = ("on", "1", "true", "yes")
_warned: set[str] = set()


def _warn_unrecognized(env_name: str, raw: str, fallback: str) -> None:
    if env_name in _warned:
        return
    _warned.add(env_name)
    print(
        f"[builder] {env_name}={raw!r} is not a recognized value (off|warn|enforce); "
        f"falling back to {fallback}. To disable this gate, spell it exactly 'off'.",
        file=sys.stderr,
    )


def gate_mode(env_name: str, default: str = "enforce") -> str:
    """Resolve a gate's staging flag to 'off' | 'warn' | 'enforce'.

    The gates are ON by default, so turning one OFF has to be spelled correctly: an
    unrecognized value resolves to the DEFAULT and warns on stderr rather than silently
    disabling the gate.  `BUILDER_HOST_VERIFY=enfroce` used to mean "no gate at all",
    which is the one way this project cannot afford to fail -- losing the guarantee has to
    be loud, and it has to be something you asked for.
    """
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    value = raw.lower()
    if value in _OFF_VALUES:
        return "off"
    if value in ("warn", "enforce"):
        return value
    _warn_unrecognized(env_name, raw, default)
    return default


def flag_enabled(env_name: str, default: bool = True) -> bool:
    """Same contract as `gate_mode` for the on/off flags: opt-out must be spelled."""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    value = raw.lower()
    if value in _OFF_VALUES:
        return False
    if value in _ON_VALUES:
        return True
    _warn_unrecognized(env_name, raw, "on" if default else "off")
    return default


def enabled() -> bool:
    return flag_enabled("BUILDER_GATE_EVIDENCE", default=True)


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    duration_ms: int = 0
    timed_out: bool = False
    spawn_error: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_bytes_total: int = 0
    stderr_bytes_total: int = 0
    truncated: bool = False
    started_at: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.spawn_error


@dataclass
class GateOutcome:
    gate: str
    verdict: str
    detail: str = ""
    mode: str = "off"
    blocking: bool = False
    reason: str = ""
    commands: list[CommandResult] = field(default_factory=list)
    bundle_path: str | None = None
    bundle_sha256: str | None = None

    def enum_string(self) -> str:
        if self.verdict == "pass":
            return "pass"
        return f"{self.verdict}:{self.detail}"


def classify_failure(result) -> str:
    """Classify a failed gate command by RETURNCODE first, stderr/stdout only to
    disambiguate within that — never a blind combined-text substring scan.

    A returncode is a structural signal the runner cannot spoof; test output text
    can legitimately CONTAIN the words "No such file or directory" (an assertion
    about a missing file) or "ImportError" (a test asserting on one) without the
    run itself being infrastructure/collection trouble. Order matters: exit 1 is
    the standard pytest/test-runner "assertions failed" code and is classified
    assertion_failure outright — text cannot overturn a clean runner failure into
    infrastructure. Infra needles are read from STDERR ONLY (the shell/runner's own
    channel); collection needles are read from STDOUT ONLY and only for exit 2
    (pytest's collection-error exit code).
    """
    if getattr(result, "timed_out", False):
        return "timeout"
    exit_code = getattr(result, "exit_code", None)
    if getattr(result, "spawn_error", "") or exit_code is None:
        return "infrastructure"
    if exit_code in (126, 127):
        return "infrastructure"
    if exit_code == 1:
        return "assertion_failure"
    stderr_text = getattr(result, "stderr_tail", "") or ""
    for needle in ("command not found", "No such file or directory", "ENOENT", "EACCES", "Permission denied"):
        if needle in stderr_text:
            return "infrastructure"
    if exit_code == 2:
        stdout_text = getattr(result, "stdout_tail", "") or ""
        for needle in (
            "ImportError",
            "ModuleNotFoundError",
            "SyntaxError",
            "error during collection",
            "errors during collection",
            "Cannot find module",
            "ReferenceError",
        ):
            if needle in stdout_text:
                return "collection_error"
    return "assertion_failure"


def read_tail(fileobj, cap) -> tuple[str, int, bool]:
    fileobj.flush()
    fileobj.seek(0, 2)
    total = fileobj.tell()
    fileobj.seek(max(0, total - cap))
    data = fileobj.read()
    if isinstance(data, str):
        text = data
    else:
        text = data.decode("utf-8", "replace")
    return text.replace("\x00", ""), total, total > cap


def _bundle_files(evidence_dir) -> list[Path]:
    d = Path(evidence_dir)
    if not d.is_dir():
        return []
    paths = [p for p in d.iterdir() if p.is_file() and re.match(r"^\d{4,}-.+\.yaml$", p.name)]
    return sorted(paths, key=lambda p: (_seq_from_path(p) or 0, p.name))


def _seq_from_path(path: Path) -> int | None:
    match = re.match(r"^(\d{4,})-", path.name)
    return int(match.group(1)) if match else None


def next_seq(evidence_dir) -> int:
    seqs = [_seq_from_path(p) for p in _bundle_files(evidence_dir)]
    seqs = [s for s in seqs if s is not None]
    return (max(seqs) + 1) if seqs else 1


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return yaml.safe_load(text) or {}


def resolve_prev_sha(evidence_dir) -> str:
    best: tuple[int, Path] | None = None
    for path in _bundle_files(evidence_dir):
        seq = _seq_from_path(path)
        if seq is None:
            continue
        if best is None or seq > best[0]:
            best = (seq, path)
    if best is None:
        return ""
    try:
        data = _load_yaml(best[1])
        return str(data.get("bundle_sha256") or "") if isinstance(data, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def bundle_sha(body: dict) -> str:
    cleaned = dict(body)
    cleaned.pop("bundle_sha256", None)
    encoded = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _yaml_roundtrips_losslessly() -> bool:
    probe = {"x": "a\nb\nverdict: pass\n", "y": "", "z": "true"}
    try:
        dumped = yaml.safe_dump(probe, sort_keys=False, allow_unicode=True)
        return yaml.safe_load(dumped) == probe
    except Exception:  # noqa: BLE001
        return False


def _serialize_bundle(out: dict) -> str | None:
    try:
        if _yaml_roundtrips_losslessly():
            serialized = yaml.safe_dump(out, sort_keys=False, allow_unicode=True)
        else:
            serialized = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        if yaml.safe_load(serialized) != out:
            return None
        return serialized
    except Exception:  # noqa: BLE001
        return None


def write_bundle(evidence_dir, body) -> Path | None:
    try:
        d = Path(evidence_dir)
        d.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            seq = next_seq(d)
            gate = str(body.get("gate") or "gate")
            phase = str(body.get("phase") or "phase")
            target = d / f"{seq:04d}-{gate}-{phase}.yaml"
            if target.exists():
                continue
            out = dict(body)
            out["seq"] = seq
            if out.get("spec_id") is not None and out.get("phase") is not None and out.get("gate") is not None:
                out["gate_id"] = f"{out['spec_id']}:{out['phase']}:{out['gate']}:{seq:04d}"
            out["prev_bundle_sha256"] = resolve_prev_sha(d)
            out["bundle_sha256"] = bundle_sha(out)
            serialized = _serialize_bundle(out)
            if serialized is None:
                return None
            fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(d))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(serialized)
                if target.exists():
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    continue
                os.replace(tmp_name, target)
                if isinstance(body, dict):
                    body.update(out)
                return target
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return None
        return None
    except Exception:  # noqa: BLE001
        return None


def verify_chain(spec_dir, expected_head_sha: str | None = None) -> list[str]:
    violations: list[str] = []
    d = Path(spec_dir) / EVIDENCE_DIRNAME
    if not d.exists():
        return ["gate-evidence directory missing"]
    if not d.is_dir():
        return ["gate-evidence path is not a directory"]
    paths = _bundle_files(d)
    if not paths:
        return ["gate-evidence directory empty"]
    seen: set[int] = set()
    prev_sha = ""
    last_stored_sha = ""
    for expected_seq, path in enumerate(paths, start=1):
        filename_seq = _seq_from_path(path)
        try:
            data = _load_yaml(path)
        except Exception as exc:  # noqa: BLE001
            violations.append(f"{path.name}: unreadable: {exc}")
            continue
        if not isinstance(data, dict):
            violations.append(f"{path.name}: not a mapping")
            continue
        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            violations.append(f"{path.name}: invalid internal seq")
        else:
            if seq in seen:
                violations.append(f"duplicate internal seq {seq}: {path.name}")
            seen.add(seq)
            if seq != expected_seq:
                violations.append(
                    f"{path.name}: non-contiguous seq: expected {expected_seq}, found {seq}")
            if filename_seq != seq:
                violations.append(
                    f"{path.name}: filename seq {filename_seq} != internal seq {seq}")
        stored = str(data.get("bundle_sha256") or "")
        computed = bundle_sha(data)
        if computed != stored:
            violations.append(f"{path.name}: sha mismatch")
        if str(data.get("prev_bundle_sha256") or "") != prev_sha:
            violations.append(f"{path.name}: prev link mismatch")
        prev_sha = computed
        last_stored_sha = stored
    if expected_head_sha is not None and last_stored_sha != str(expected_head_sha):
        violations.append(
            f"expected head mismatch: expected {expected_head_sha}, found {last_stored_sha}")
    return violations


def dispatcher_version() -> str:
    global _DISPATCHER_VERSION
    if _DISPATCHER_VERSION is not None:
        return _DISPATCHER_VERSION
    env_v = os.environ.get("BUILDER_DISPATCHER_VERSION")
    if env_v:
        _DISPATCHER_VERSION = env_v
        return _DISPATCHER_VERSION
    try:
        here = Path(__file__).resolve().parent
        with tempfile.TemporaryFile() as stdout_file:
            res = subprocess.run(
                ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                stdout=stdout_file,
                stderr=subprocess.DEVNULL,
                timeout=GIT_EVIDENCE_TIMEOUT,
            )
            stdout, _total, _truncated = read_tail(stdout_file, TAIL_BYTES)
        _DISPATCHER_VERSION = stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        _DISPATCHER_VERSION = ""
    return _DISPATCHER_VERSION


def host_info() -> dict:
    try:
        hostname = socket.gethostname() or ""
    except Exception:  # noqa: BLE001
        hostname = ""
    return {"hostname": hostname, "dispatcher_version": dispatcher_version()}


def main(argv) -> int:
    args = list(argv or [])
    if not args:
        print("usage: gate_evidence.py SPEC_DIR", file=sys.stderr)
        return 2
    violations = verify_chain(args[0])
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
