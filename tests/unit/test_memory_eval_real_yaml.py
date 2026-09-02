"""Regression guard for the memory_eval YAML string-fidelity bug.

The bug: real PyYAML (6.x = YAML 1.1) reparses an unquoted `memory_mode: off`
as the bool `False` and an unquoted `ts: ...Z` as a `datetime`; the schema
`enum: [off, ...]` likewise loaded `off` as `False`. Both broke the
`type: string` validation in `append_memory_eval` / `load_memory_evals`, but the
failure was MASKED by the repo-root shadow `yaml.py` that wins on sys.path under
the local ./pytest runner and `python3 -c`.

So these tests SPAWN A SUBPROCESS WITH `-P` (PYTHONSAFEPATH) — that drops the
script's directory / cwd from sys.path, so the shadow `yaml.py` is invisible and
the real PyYAML in the venv is imported. If the string-fidelity layer regresses,
these tests fail; the shadow can no longer hide it.

If PyYAML is not installed, the subprocess falls back to the in-repo parser and
the round trip still holds (the fallback writer now quotes the ambiguous words),
so the test is meaningful in both environments.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
GAIN_CLI = SCRIPTS_DIR / "builder-memory-gain.py"


def _run_isolated(code: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `code` in a subprocess with sys.path isolation (-P), so the repo-root
    shadow yaml.py cannot shadow the real PyYAML. scripts/ is added explicitly so
    the telemetry package is importable, mirroring the spawned-script runner path
    (sys.path[0] = scripts/)."""
    prelude = f"import sys; sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
    return subprocess.run(
        [sys.executable, "-P", "-c", prelude + code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _assert_real_yaml_or_fallback(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, (
        f"isolated subprocess failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_append_load_round_trip_under_real_pyyaml(tmp_path):
    """build -> append -> load round-trips `memory_mode='off'` and `ts` as
    strings even when imported under real PyYAML (no shadow on sys.path)."""
    code = f"""
from pathlib import Path
from _yaml import yaml
# Prove the shadow is NOT in play: real PyYAML has a version; the shadow does not.
assert hasattr(yaml, "__version__"), "expected REAL PyYAML, got the shadow yaml.py"
from _telemetry.memory_eval import build_memory_eval, append_memory_eval, load_memory_evals
ws = Path({str(tmp_path)!r})
rec = build_memory_eval(run_id="r1", spec_id="s1", lane="claude",
    plan_tokens_in=100, plan_tokens_out=1500, plan_wall_ms=5000,
    spec_outcome="verified", memory_mode="off")
append_memory_eval(ws, rec)
rows = load_memory_evals(ws)
assert len(rows) == 1, rows
assert rows[0]["memory_mode"] == "off", repr(rows[0]["memory_mode"])
assert isinstance(rows[0]["ts"], str), type(rows[0]["ts"]).__name__
assert rows[0]["ts"].endswith("Z"), rows[0]["ts"]
print("ROUND_TRIP_OK")
"""
    proc = _run_isolated(code, cwd=tmp_path)
    _assert_real_yaml_or_fallback(proc)
    assert "ROUND_TRIP_OK" in proc.stdout, proc.stdout


def test_legacy_unquoted_file_is_coerced_under_real_pyyaml(tmp_path):
    """A pre-fix on-disk file with UNQUOTED `memory_mode: off` / `ts: ...Z` is
    coerced back to strings on read instead of being rejected."""
    day = tmp_path / ".builder" / "telemetry" / "events" / "memory-eval" / "2026-06-08"
    day.mkdir(parents=True)
    (day / "MEVAL-legacy.yaml").write_text(
        "artifact: memory_eval\n"
        "ts: 2026-06-08T12:00:00Z\n"
        "run_id: r1\n"
        "spec_id: s1\n"
        "phase: 4-plan\n"
        "lane: claude\n"
        "memory_mode: off\n"
        "plan_tokens_in: 100\n"
        "plan_tokens_out: 2000\n"
        "plan_wall_ms: 5000\n"
        "recall_calls: 0\n"
        "recall_hits: 0\n"
        "recall_latency_ms: 0\n"
        "decisions_reused: 0\n"
        "decisions_written: 0\n"
        "spec_outcome: verified\n",
        encoding="utf-8",
    )
    code = f"""
from pathlib import Path
from _telemetry.memory_eval import load_memory_evals
rows = load_memory_evals(Path({str(tmp_path)!r}))
assert len(rows) == 1, rows
assert rows[0]["memory_mode"] == "off", repr(rows[0]["memory_mode"])
assert rows[0]["ts"] == "2026-06-08T12:00:00Z", repr(rows[0]["ts"])
print("LEGACY_OK")
"""
    proc = _run_isolated(code, cwd=tmp_path)
    _assert_real_yaml_or_fallback(proc)
    assert "LEGACY_OK" in proc.stdout, proc.stdout


def test_gain_report_cli_as_spawned_script(tmp_path):
    """The full failing flow: inject both arms via append_memory_eval, then run
    builder-memory-gain.py report AS A SPAWNED SCRIPT (sys.path[0]=scripts/,
    real PyYAML). The report must render a finite off-vs-hivemind delta."""
    # Seed both arms in an isolated subprocess so the sink files are written by
    # the real-PyYAML writer (matching production).
    seed = f"""
from pathlib import Path
from _telemetry.memory_eval import build_memory_eval, append_memory_eval
ws = Path({str(tmp_path)!r})
for i in range(2):
    append_memory_eval(ws, build_memory_eval(run_id=f"off-{{i}}", spec_id=f"s{{i}}",
        lane="claude", plan_tokens_in=200, plan_tokens_out=1500, plan_wall_ms=6000,
        spec_outcome="verified", memory_mode="off"))
for i in range(2):
    append_memory_eval(ws, build_memory_eval(run_id=f"hive-{{i}}", spec_id=f"s{{i}}",
        lane="claude", plan_tokens_in=200, plan_tokens_out=1000, plan_wall_ms=4500,
        spec_outcome="verified", memory_mode="hivemind",
        recall_calls=2, recall_hits=2, decisions_reused=3, decisions_written=1))
print("SEEDED")
"""
    seed_proc = _run_isolated(seed, cwd=tmp_path)
    _assert_real_yaml_or_fallback(seed_proc)
    assert "SEEDED" in seed_proc.stdout, seed_proc.stdout

    # Now run the report CLI exactly as the runner's do_report() spawns it.
    out_path = tmp_path / "report.md"
    proc = subprocess.run(
        [sys.executable, str(GAIN_CLI), "report",
         "--root", str(tmp_path), "--out", str(out_path), "--no-telegram"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"report CLI failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    report = out_path.read_text(encoding="utf-8")
    assert "off" in report and "hivemind" in report, report
    # Finite, non-trivial delta: hivemind 1000 vs off 1500 => -500 abs, -33.3% pct.
    assert "-500.000" in report, report
    assert "-33.3%" in report, report
    # recall_hit_rate must differentiate the arms (off 0.0, hivemind 1.0).
    assert "1.000" in report, report
