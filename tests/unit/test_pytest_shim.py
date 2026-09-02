"""The harness must not lie about what it ran.

This repo vendors a minimal `pytest` shim, and every gate command in the project is executed
through it. It used to SILENTLY DROP a named path that did not exist and still print "N passed"
-- so a gate command naming a renamed or deleted test file kept reporting green while testing
strictly less than it claimed. A vacuous check reported as a success is the exact laundering
this project exists to refuse, and the harness is the last place that can be allowed to do it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _shim():
    spec = importlib.util.spec_from_file_location("pytest_shim_under_test", ROOT / "pytest" / "__main__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_path_is_a_hard_error_not_a_silent_pass(tmp_path):
    shim = _shim()
    try:
        shim._paths([str(tmp_path / "test_nope.py")])
    except SystemExit as exc:
        assert exc.code == 4, "a named test file that does not exist must fail the run"
        return
    raise AssertionError("a nonexistent test path was silently dropped -- the run would go green")


def test_existing_paths_still_collect(tmp_path):
    shim = _shim()
    f = tmp_path / "test_thing.py"
    f.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert shim._paths([str(f)]) == [f]


def test_a_directory_collects_its_test_files(tmp_path):
    shim = _shim()
    (tmp_path / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tmp_path / "not_a_test.py").write_text("x = 1\n", encoding="utf-8")
    assert shim._paths([str(tmp_path)]) == [tmp_path / "test_a.py"]


def test_flag_values_are_not_mistaken_for_paths(tmp_path):
    # `-k demo` would otherwise treat "demo" as a test path -- and, now that a missing path is a
    # hard error, that would break every -k invocation in the corpus.
    shim = _shim()
    f = tmp_path / "test_thing.py"
    f.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert shim._paths([str(f), "-k", "demo", "-q", "-o", 'addopts=""']) == [f]


def test_inline_flag_value_consumes_nothing(tmp_path):
    shim = _shim()
    f = tmp_path / "test_thing.py"
    f.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert shim._paths(["--tb=line", str(f)]) == [f]


def test_keyword_expression_filters_node_ids_without_running_unselected_tests():
    shim = _shim()
    assert shim._matches_keyword("sync_readmit", "tests/unit/test_isanna.py::test_sync_readmit_refuses")
    assert not shim._matches_keyword("sync_readmit", "tests/unit/test_isanna.py::test_release_status")
    assert shim._matches_keyword(
        "sync and (readmit or release)",
        "tests/unit/test_isanna.py::test_sync_readmit_refuses",
    )


def test_keyword_flag_is_extracted_in_separate_and_inline_forms():
    shim = _shim()
    assert shim._keyword_expression(["test_x.py", "-k", "readmit", "-q"]) == "readmit"
    assert shim._keyword_expression(["test_x.py", "-k=readmit"]) == "readmit"


def test_conftest_is_a_hard_error_not_silently_ignored(tmp_path):
    (tmp_path / "conftest.py").write_text("# real pytest configuration\n", encoding="utf-8")
    (tmp_path / "test_thing.py").write_text("def test_thing():\n    pass\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "pytest" / "__main__.py"), str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 4
    assert str(tmp_path / "conftest.py") in result.stderr
    assert "does not implement conftest" in result.stderr


def test_parent_conftest_is_a_hard_error_for_a_named_leaf_test(tmp_path):
    (tmp_path / "conftest.py").write_text("# real pytest configuration\n", encoding="utf-8")
    leaf = tmp_path / "nested" / "test_thing.py"
    leaf.parent.mkdir()
    leaf.write_text("def test_thing():\n    pass\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "pytest" / "__main__.py"), str(leaf)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 4
    assert str(tmp_path / "conftest.py") in result.stderr


def test_pyproject_pytest_options_are_a_hard_error_not_silently_ignored(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
    (tmp_path / "test_thing.py").write_text("def test_thing():\n    pass\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "pytest" / "__main__.py"), str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 4
    assert str(tmp_path / "pyproject.toml") in result.stderr
    assert "does not implement pytest configuration" in result.stderr
