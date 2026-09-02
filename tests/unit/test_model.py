"""Tests for scripts/model.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "model.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("model", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


model_mod = _load_module()


def _write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    root = Path(tmp_path)
    (root / ".builder" / "specs").mkdir(parents=True)
    return root


def _spec(root: Path, spec_id: str, *, status="verified") -> Path:
    spec = root / ".builder" / "specs" / spec_id
    _write_yaml(spec / "spec.yaml", {"name": spec_id, "status": status})
    return spec


def _tasks(spec: Path, tasks: list[dict]) -> None:
    _write_yaml(spec / "tasks.yaml", {"tasks": tasks})


def _requirements(spec: Path, acceptance: list) -> None:
    _write_yaml(spec / "requirements.yaml", {"requirements": [{"id": "R1", "acceptance": acceptance}]})


def _trace(spec: Path, file_path: str, value: str) -> None:
    _write_yaml(
        spec / "traceability.yaml",
        {
            "task_links": [
                {
                    "task_id": "T1",
                    "files": [
                        {
                            "path": file_path,
                            "anchors": [{"id": "A1", "kind": "literal_string", "locator": value}],
                        }
                    ],
                }
            ]
        },
    )


def _build(root: Path) -> dict:
    return model_mod.build_model(root)


def _result(command: str, exit_code: int, *, stdout="", stderr="", duration=7, spawn_error="", timed_out=False):
    return model_mod.CommandResult(
        command=command,
        exit_code=exit_code,
        duration_ms=duration,
        timed_out=timed_out,
        spawn_error=spawn_error,
        stdout_tail=stdout,
        stderr_tail=stderr,
    )


class Runner:
    def __init__(self, exit_code=0, *, stdout="1 passed", stderr="", spawn_error="", timed_out=False):
        self.calls = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.spawn_error = spawn_error
        self.timed_out = timed_out

    def __call__(self, commands, cwd):
        self.calls.append((tuple(commands), cwd))
        return [_result(commands[0], self.exit_code, stdout=self.stdout, stderr=self.stderr, spawn_error=self.spawn_error, timed_out=self.timed_out)]


def test_build_harvests_task_verify_commands(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/test_demo.py -q", "proves": ["AC-R1-1"]}]}])

    model = _build(root)

    cap = model["capabilities"][0]
    assert cap["granularity"] == "task_verify"
    assert cap["checks"][0]["id"] == "T1"
    assert cap["checks"][0]["command"] == "python3 -m pytest tests/test_demo.py -q"
    assert cap["checks"][0]["proves"] == ["AC-R1-1"]


def test_build_prefers_ac_oracle_when_present(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(
        spec,
        [{"id": "AC-R1-1", "oracle": {"type": "automated_test", "expected": "`python3 -m pytest tests/test_ac.py -q` exits 0"}}],
    )
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/test_task.py -q"}]}])

    model = _build(root)

    cap = model["capabilities"][0]
    assert cap["granularity"] == "ac_oracle"
    assert any(check["source"] == "requirements.yaml" and check["id"] == "AC-R1-1" for check in cap["checks"])


def test_build_reads_every_command_from_canonical_expected(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(
        spec,
        [{
            "id": "AC-R1-1",
            "oracle": {
                "type": "automated_test",
                "expected": "`python3 -m pytest tests/a.py -q` and `python3 -m pytest tests/b.py -q` both exit 0",
            },
        }],
    )

    model = _build(root)

    checks = model["capabilities"][0]["checks"]
    assert [check["command"] for check in checks] == [
        "python3 -m pytest tests/a.py -q",
        "python3 -m pytest tests/b.py -q",
    ]


def test_empty_ac_metadata_falls_back_to_task_checks(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(spec, [{"id": "AC-R1-1", "oracle": {"type": "automated_test", "expected": "suite passes"}}])
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/task.py -q"}]}])

    model = _build(root)

    cap = model["capabilities"][0]
    assert cap["granularity"] == "task_verify"
    assert [check["source"] for check in cap["checks"]] == ["tasks.yaml"]


def test_unknown_oracle_command_is_excluded_from_machine_scoring(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(
        spec,
        [{"id": "AC-R1-1", "oracle": {"type": "unknown", "expected": "`python3 probe.py` exits 0"}}],
    )
    _build(root)
    runner = Runner()

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:oracle"] == 1
    assert report["machine_denominator"] == 0


def test_build_marks_spec_with_no_checks_as_claimed(tmp_path):
    root = _repo(tmp_path)
    _spec(root, "demo")

    model = _build(root)
    report, text = model_mod.verify_model(root, execute=True, runner=Runner())

    assert model["capabilities"][0]["granularity"] == "none"
    assert report["counts"]["claimed"] == 1
    assert "claimed" in text


def test_non_probative_check_excluded_and_flagged(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "exit 0"}]}])

    model = _build(root)
    report, text = model_mod.verify_model(root, execute=True, runner=Runner())

    check = model["capabilities"][0]["checks"][0]
    assert check["non_probative"] is True
    assert report["counts"]["non_probative"] == 1
    assert report["machine_denominator"] == 0
    assert "NOTHING was checked" in text


def test_zero_denominator_summary_says_nothing_was_verified(tmp_path):
    root = _repo(tmp_path)
    _spec(root, "demo")
    _build(root)

    report, text = model_mod.verify_model(root, execute=True, runner=Runner())

    assert report["machine_denominator"] == 0
    assert "0/0 machine-checkable capabilities currently hold" not in text
    assert "NOTHING was checked" in text
    assert "blind spot, not a clean bill of health" in text


def test_verify_dedupes_by_command_and_cwd(tmp_path):
    root = _repo(tmp_path)
    for spec_id in ("a", "b"):
        spec = _spec(root, spec_id)
        _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/shared.py -q"}]}])
    _build(root)
    runner = Runner()

    report, text = model_mod.verify_model(root, execute=True, runner=runner)
    ledger = model_mod._latest_ledger_entries(root, None)

    assert len(runner.calls) == 1
    assert report["counts"]["proven"] == 2
    assert len(ledger) == 1
    assert sorted(ledger[0]["proves"]) == ["cap:a/T1", "cap:b/T1"]


def test_verify_passing_command_marks_proven(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]}])
    _build(root)

    report, text = model_mod.verify_model(root, execute=True, runner=Runner(exit_code=0))

    assert report["counts"]["proven"] == 1
    assert "1/1 machine-checkable capabilities currently hold" in text


def test_vacuous_test_runs_are_never_proven(tmp_path):
    root = _repo(tmp_path)
    outputs = {
        "tests/no_tests.py": "no tests ran in 0.01s",
        "tests/zero.py": "collected 0 items",
        "tests/skipped.py": "3 skipped in 0.02s",
        "tests/deselected.py": "4 deselected in 0.02s",
        "tests/empty.py": "",
        "tests/zero_passed.py": "0 passed in 0.01s",
    }
    for index, path in enumerate(outputs, start=1):
        spec = _spec(root, f"demo-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": f"python3 -m pytest {path} -q"}]}])
    _build(root)

    def runner(commands, cwd):
        command = commands[0]
        output = next(value for path, value in outputs.items() if path in command)
        return [_result(command, 0, stdout=output)]

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert report["counts"]["vacuous"] == len(outputs)
    assert report["counts"]["proven"] == 0
    assert report["machine_denominator"] == 0
    assert "ran nothing" in text


def test_verify_failing_command_marks_broken(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/fail.py -q"}]}])
    _build(root)

    report, text = model_mod.verify_model(root, execute=True, runner=Runner(exit_code=1, stderr="assert 1 == 2"))

    assert report["counts"]["broken"] == 1
    assert "BROKEN - these capabilities no longer work" in text
    assert "cap:demo   T1   assertion_failure" in text


def test_collection_rot_is_check_rotted_not_broken(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/moved.py -q"}]}])
    _build(root)

    report, text = model_mod.verify_model(
        root,
        execute=True,
        runner=Runner(exit_code=2, stderr="ImportError while importing test module\nerror during collection"),
    )

    assert report["counts"]["check_rotted"] == 1
    assert report["counts"]["broken"] == 0
    assert "CHECK ROTTED" in text
    assert "BROKEN -" not in text


def test_human_only_is_unverifiable_not_broken(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(spec, [{"id": "AC-R1-1", "oracle": {"type": "human_only", "expected": "`python3 probe.py` exits 0"}}])
    _build(root)

    report, text = model_mod.verify_model(root, execute=True, runner=Runner(exit_code=1))

    assert report["counts"]["unverifiable"] == 1
    assert report["counts"]["broken"] == 0
    assert report["machine_denominator"] == 0
    assert "+1 unverifiable" in text


def test_infrastructure_failure_is_clustered(tmp_path):
    root = _repo(tmp_path)
    for spec_id in ("a", "b", "c"):
        spec = _spec(root, spec_id)
        _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest needs_db.py -q"}]}])
    _build(root)

    report, text = model_mod.verify_model(root, execute=True, runner=Runner(exit_code=127, stderr="command not found: postgres"))

    assert report["counts"]["broken"] == 0
    assert report["counts"]["unverifiable"] == 3
    assert len(report["infrastructure"]) == 1
    assert report["infrastructure"][0]["count"] == 3
    assert text.count("INFRASTRUCTURE") == 1


def test_timeout_is_retried_and_never_broken(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/slow.py -q"}]}])
    _build(root)
    runner = Runner(exit_code=1, stderr="timed out", timed_out=True)

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert len(runner.calls) == 2
    assert report["counts"]["timeout"] == 1
    assert report["counts"]["broken"] == 0
    assert "TIMEOUT" in text


def test_bounded_probe_preconditions_are_evaluated(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(
        spec,
        [{
            "id": "AC-R1-1",
            "oracle": {
                "type": "bounded_probe",
                "expected": "`python3 -m pytest tests/probe.py` exits 0",
                "preconditions": ["python3 -m pytest tests/precondition.py"],
            },
        }],
    )
    _build(root)
    runner = Runner()

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert [call[0][0] for call in runner.calls] == [
        "python3 -m pytest tests/precondition.py",
        "python3 -m pytest tests/probe.py",
    ]
    assert report["counts"]["proven"] == 1


def test_unmet_bounded_probe_precondition_is_unverifiable(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _requirements(
        spec,
        [{
            "id": "AC-R1-1",
            "oracle": {
                "type": "bounded_probe",
                "expected": "`python3 -m pytest tests/probe.py` exits 0",
                "preconditions": ["python3 -m pytest tests/precondition.py"],
            },
        }],
    )
    _build(root)

    def runner(commands, cwd):
        return [_result(commands[0], 1, stderr="not ready")]

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert report["counts"]["unverifiable:precondition"] == 1
    assert report["counts"]["broken"] == 0


def test_capability_rollup_is_minimum(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(
        spec,
        [
            {"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]},
            {"id": "T2", "verify": [{"command": "python3 -m pytest tests/fail.py -q"}]},
        ],
    )
    _build(root)

    def runner(commands, cwd):
        code = 1 if "fail.py" in commands[0] else 0
        return [_result(commands[0], code, stderr="assert failed" if code else "")]

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert report["counts"]["broken"] == 1
    assert "cap:demo   T2   assertion_failure" in text


def test_drift_dead_anchor_passing_oracle_is_not_red(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]}])
    _trace(spec, "src/old.py", "def live_feature")
    (root / "src").mkdir()
    (root / "src" / "new.py").write_text("def live_feature():\n    return True\n", encoding="utf-8")
    _build(root)
    model_mod.verify_model(root, execute=True, runner=Runner(exit_code=0))

    report, text = model_mod.drift_model(root)

    assert report["findings"][0]["severity"] == "low"
    assert "HIGH" not in text
    assert "moved" in text


def test_drift_dead_anchor_failing_oracle_is_functionality_lost(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/fail.py -q"}]}])
    _trace(spec, "src/old.py", "def live_feature")
    _build(root)
    model_mod.verify_model(root, execute=True, runner=Runner(exit_code=1, stderr="assert failed"))

    report, text = model_mod.drift_model(root)

    assert report["findings"][0]["severity"] == "high"
    assert "SUSPECTED FUNCTIONALITY LOST" in text
    assert "resolution must be a spec" in text


def test_ledger_is_host_written(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/fail.py -q"}]}])
    _build(root)
    model_mod.verify_model(root, execute=True, runner=Runner(exit_code=5, stdout="tail-data"))

    ledger = model_mod._latest_ledger_entries(root, None)

    assert ledger[0]["source"] == "host"
    assert ledger[0]["exit_code"] == 5
    assert "tail-data" in ledger[0]["stdout_tail"]


def test_each_run_gets_an_immutable_ledger_file(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]}])
    _build(root)

    model_mod.verify_model(root, execute=True, runner=Runner())
    model_mod.verify_model(root, execute=True, runner=Runner())

    ledgers = sorted((root / ".builder" / "model" / "verification").rglob("*.yaml"))
    assert len(ledgers) == 2
    assert all(len(model_mod._read_ledger(path)) == 1 for path in ledgers)


def test_verify_is_dry_run_by_default(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    (root / "test_marker.py").write_text(
        "import pathlib, unittest\n"
        "class MarkerTest(unittest.TestCase):\n"
        "    def test_marker(self): pathlib.Path('marker').write_text('ran')\n",
        encoding="utf-8",
    )
    _tasks(
        spec,
        [{"id": "T1", "verify": [{"command": "python3 -m unittest test_marker.py"}]}],
    )
    _build(root)

    report, text = model_mod.verify_model(root)

    assert report["mode"] == "dry-run"
    assert report["commands_executed"] == 0
    assert not (root / "marker").exists()
    assert "DRY RUN - no commands executed" in text
    assert "WOULD RUN" in text


def test_execute_runs_a_real_hermetic_command(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    (root / "test_marker.py").write_text(
        "import pathlib, unittest\n"
        "class MarkerTest(unittest.TestCase):\n"
        "    def test_marker(self): pathlib.Path('marker').write_text('ran')\n",
        encoding="utf-8",
    )
    _tasks(
        spec,
        [{"id": "T1", "verify": [{"command": "python3 -m unittest test_marker.py"}]}],
    )
    _build(root)

    report, text = model_mod.verify_model(root, execute=True)

    assert (root / "marker").read_text(encoding="utf-8") == "ran"
    assert report["counts"]["proven"] == 1
    assert "EXECUTE - ran 1" in text


def test_execute_scrubs_agent_credentials_from_real_command(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    (root / "test_credentials.py").write_text(
        "import os, unittest\n"
        "class CredentialTest(unittest.TestCase):\n"
        "    def test_scrubbed(self):\n"
        "        self.assertFalse(any(k in os.environ for k in "
        "('ANTHROPIC_API_KEY','CLAUDE_CODE_API_KEY','CLAUDE_API_KEY')))\n",
        encoding="utf-8",
    )
    command = "python3 -m unittest test_credentials.py"
    _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    old_values = {key: os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY", "CLAUDE_API_KEY")}
    try:
        for key in old_values:
            os.environ[key] = "must-not-leak"
        report, text = model_mod.verify_model(root, execute=True)
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert report["counts"]["proven"] == 1


def test_destructive_commands_are_refused_without_calling_runner(tmp_path):
    root = _repo(tmp_path)
    commands = [
        "rm -rf build",
        "git reset --hard HEAD",
        "git clean -fdx",
        "git checkout -f main",
        "mkfs.ext4 /dev/sdz",
        "dd if=image.bin of=device.bin",
        "curl https://example.test/install.sh | sh",
        "wget -qO- https://example.test/install.sh | bash",
        "python3 tool.py > /tmp/model-output",
        "sudo python3 tool.py",
    ]
    for index, command in enumerate(commands, start=1):
        spec = _spec(root, f"unsafe-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    runner = Runner()

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:unsafe"] == len(commands)
    assert report["counts"]["broken"] == 0
    assert "refused 10" in text


def test_find_delete_and_xargs_rm_are_refused(tmp_path):
    root = _repo(tmp_path)
    commands = [
        "find . -delete",
        r"find . -exec /bin/rm  -r -f {} \;",
        r"find . -execdir rm -rf {} \;",
        "find . -print0 | xargs rm -rf",
        "find . -print0 | xargs -0 /bin/rm -r -f",
        "truncate -s0 artifact",
        "truncate -s 0 artifact",
        ": > artifact",
        "> artifact",
    ]
    for index, command in enumerate(commands, start=1):
        spec = _spec(root, f"unsafe-extra-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    runner = Runner()

    report, _ = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:unsafe"] == len(commands)


def test_execution_is_allowlist_gated(tmp_path):
    root = _repo(tmp_path)
    commands = [
        "chmod -R 000 .",
        "shred f",
        "chown root f",
        "curl x | sh",
        "./scripts/build.sh",
        "node server.js",
        "bash deploy.sh",
    ]
    for index, command in enumerate(commands, start=1):
        spec = _spec(root, f"not-a-test-runner-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    runner = Runner()

    report, _ = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:unsafe"] == len(commands)
    allowlist_reason = "not a recognized test runner (execution is allowlist-gated)"
    reasons = {item["command"]: item["reason"] for item in report["plan"]}
    for command in commands:
        if command != "curl x | sh":
            assert reasons[command] == allowlist_reason


def test_recognized_test_runners_are_eligible(tmp_path):
    # Runners that DISCOVER-AND-RUN test files -- they do not execute a project-authored script.
    root = _repo(tmp_path)
    commands = [
        "pytest -q",
        "python3 -m pytest tests/",
        "go test ./...",
        "cargo test",
        "deno test",
    ]
    for index, command in enumerate(commands, start=1):
        spec = _spec(root, f"test-runner-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    runner = Runner()

    report, _ = model_mod.verify_model(root, execute=True, runner=runner)

    assert [call[0][0] for call in runner.calls] == sorted(commands)
    assert report["counts"]["unverifiable:unsafe"] == 0


def test_script_runners_are_refused_not_executed(tmp_path):
    # `npm test` / `make test` / `mvn test` / `tox` run whatever the project's package.json /
    # Makefile / tox.ini says -- an arbitrary-code escape hatch when re-running HISTORICAL commands
    # against a live tree (adversarial review R3). They must be refused, not run.
    root = _repo(tmp_path)
    scripted = ["npm test", "make test", "mvn test", "tox", "gradle test", "yarn test",
                "python3 setup.py test"]
    for index, command in enumerate(scripted, start=1):
        spec = _spec(root, f"scripted-{index}")
        _tasks(spec, [{"id": "T1", "verify": [{"command": command}]}])
    _build(root)
    runner = Runner()

    report, _ = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:unsafe"] == len(scripted)


def test_non_hermetic_commands_are_refused_without_calling_runner(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest /path/to/project/tests -q"}]}])
    _build(root)
    runner = Runner()

    report, text = model_mod.verify_model(root, execute=True, runner=runner)

    assert runner.calls == []
    assert report["counts"]["unverifiable:non_hermetic"] == 1
    assert report["machine_denominator"] == 0


def test_malformed_tasks_becomes_collection_finding_not_claimed(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    (spec / "tasks.yaml").write_text("tasks: [unterminated", encoding="utf-8")

    model = _build(root)
    report, text = model_mod.verify_model(root)

    assert model["collection_findings"][0]["kind"] in {"parse_error", "type_error"}
    assert report["counts"]["check_rotted"] == 1
    assert report["counts"]["claimed"] == 0
    assert "CHECK ROTTED" in text


def test_missing_and_invalid_models_fail_closed(tmp_path):
    missing_root = _repo(tmp_path / "missing")
    try:
        model_mod.verify_model(missing_root)
        missing_error = ""
    except model_mod.ModelDataError as exc:
        missing_error = str(exc)
    assert "system model missing" in missing_error

    invalid_root = _repo(tmp_path / "invalid")
    _write_yaml(
        invalid_root / ".builder" / "model" / "system-model.yaml",
        {"schema": model_mod.SCHEMA, "repo": "invalid", "capabilities": None},
    )
    try:
        model_mod.verify_model(invalid_root)
        invalid_error = ""
    except model_mod.ModelDataError as exc:
        invalid_error = str(exc)
    assert "capabilities must be a list" in invalid_error


def test_invalid_anchor_regex_is_not_high_severity(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _trace(spec, "src/demo.py", "[")
    trace = json.loads((spec / "traceability.yaml").read_text(encoding="utf-8"))
    trace["task_links"][0]["files"][0]["anchors"][0]["kind"] = "regex_v1"
    _write_yaml(spec / "traceability.yaml", trace)
    (root / "src").mkdir()
    (root / "src" / "demo.py").write_text("demo\n", encoding="utf-8")
    _build(root)

    report, text = model_mod.drift_model(root)

    assert report["findings"][0]["severity"] == "invalid"
    assert "INVALID ANCHOR REGEX" in text
    assert "HIGH" not in text


def test_never_writes_into_a_spec_dir(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]}])
    before = sorted(path.relative_to(spec).as_posix() for path in spec.rglob("*"))

    _build(root)
    model_mod.verify_model(root, execute=True, runner=Runner(exit_code=0))
    after = sorted(path.relative_to(spec).as_posix() for path in spec.rglob("*"))

    assert before == after


def test_banned_strings_absent(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/pass.py -q"}]}])
    _trace(spec, "src/missing.py", "needle")
    _build(root)
    report, verify_text = model_mod.verify_model(root, execute=True, runner=Runner(exit_code=0))
    drift_report, drift_text = model_mod.drift_model(root)
    combined = json.dumps(report) + verify_text + json.dumps(drift_report) + drift_text

    for banned in model_mod.BANNED_STRINGS:
        assert banned not in combined.lower()


def test_direct_js_runners_are_eligible_but_package_scripts_are_not():
    """`pnpm exec vitest` resolves a BINARY; `pnpm test` runs a project-authored script.

    The allowlist comment called jest/vitest/mocha "safe in principle but omitted for now -- add
    them once confirmed they cannot be pointed at a shelling config". That condition is discharged
    by refusing an explicit --config/-c/--project, since a runner config file is executable code.
    The npm/pnpm/yarn SCRIPT exclusion is unchanged and load-bearing: a recorded `pnpm test` re-run
    against a live tree executes whatever the current package.json says.
    """
    accept = [
        "pnpm exec vitest run apps/web",
        "pnpm --filter @acme/web exec vitest run",
        "npx vitest run apps/web",
        "vitest run apps/web",
        "jest --runInBand",
        "node --test scripts/boundaries.test.mjs",
    ]
    refuse = [
        "pnpm test",
        "pnpm run test",
        "pnpm check:app-boundaries",
        "pnpm --filter @acme/web build",
        "npm test",
        "yarn test",
        "make test",
        "node scripts/catalog-validate.mjs",
        "pnpm dlx vitest run",
        "npx --package evil vitest run",
        "pnpm exec vitest run --config /tmp/evil.ts",
        "vitest run -c /tmp/evil.ts",
        "vitest run --project=/tmp/evil",
    ]
    for command in accept:
        assert model_mod._execution_allowlist_reason(command) is None, command
    for command in refuse:
        assert model_mod._execution_allowlist_reason(command) is not None, command
