from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
import traceback
from unittest import SkipTest
from pathlib import Path


# Flags whose NEXT argument is a value, not a test path (`-k expr`, `-o addopts=""`). The
# `--flag=value` form carries its own value and consumes nothing.
_VALUE_FLAGS = {"-k", "-m", "-o", "-p", "-n", "-c", "--maxfail", "--tb", "--rootdir"}


def _keyword_expression(args: list[str]) -> str | None:
    """Return the last pytest ``-k`` expression, if any."""
    expression = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-k":
            if index + 1 >= len(args):
                raise ValueError("argument -k: expected one argument")
            expression = args[index + 1]
            index += 2
            continue
        if arg.startswith("-k="):
            expression = arg[3:]
        index += 1
    if expression is not None and not expression.strip():
        raise ValueError("argument -k: expected a non-empty expression")
    return expression


def _matches_keyword(expression: str | None, node_id: str) -> bool:
    """Evaluate the small, documented pytest ``-k`` boolean expression language.

    Keyword atoms match case-insensitively against the collected node id. Supporting
    ``and``, ``or``, ``not``, and parentheses keeps selection honest without evaluating
    caller text as Python.
    """
    if expression is None:
        return True
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    position = 0
    haystack = node_id.lower()

    def parse_or() -> bool:
        nonlocal position
        value = parse_and()
        while position < len(tokens) and tokens[position].lower() == "or":
            position += 1
            right = parse_and()
            value = value or right
        return value

    def parse_and() -> bool:
        nonlocal position
        value = parse_not()
        while position < len(tokens) and tokens[position].lower() == "and":
            position += 1
            right = parse_not()
            value = value and right
        return value

    def parse_not() -> bool:
        nonlocal position
        if position < len(tokens) and tokens[position].lower() == "not":
            position += 1
            return not parse_not()
        return parse_atom()

    def parse_atom() -> bool:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("unexpected end of -k expression")
        token = tokens[position]
        position += 1
        if token == "(":
            value = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unbalanced parentheses in -k expression")
            position += 1
            return value
        if token == ")" or token.lower() in {"and", "or"}:
            raise ValueError(f"unexpected token in -k expression: {token}")
        return token.lower() in haystack

    matched = parse_or()
    if position != len(tokens):
        raise ValueError(f"unexpected token in -k expression: {tokens[position]}")
    return matched


def _paths(args: list[str]) -> list[Path]:
    selected: list[Path] = []
    expect_value = False
    for arg in args:
        if expect_value:
            expect_value = False
            continue
        if arg.startswith("-"):
            expect_value = arg in _VALUE_FLAGS
            continue
        selected.append(Path(arg))
    if not selected:
        selected = [Path("tests")]

    files: list[Path] = []
    missing: list[Path] = []
    for item in selected:
        if item.is_dir():
            files.extend(sorted(item.rglob("test_*.py")))
        elif item.is_file():
            files.append(item)
        else:
            missing.append(item)

    # A named path that does not exist was SILENTLY DROPPED here, and the run still printed
    # "N passed". That is a vacuous check reported as a success -- rename or delete a test file
    # and the gate command that names it keeps going green while testing strictly less. It is
    # the exact laundering this project exists to refuse, sitting in our own harness. Real pytest
    # exits 4 on a missing path; so do we.
    if missing:
        for item in missing:
            print(f"pytest: no such file or directory: {item}", file=sys.stderr)
        raise SystemExit(4)
    return files


def _unsupported_pytest_setup(args: list[str]) -> Path | None:
    """Return the first pytest feature this deliberately small runner cannot honor.

    A conftest.py or pytest.ini_options section is executable test configuration under
    real pytest. Continuing without it would report a green run for a different test
    suite, so reject it before importing a test module.
    """
    selected: list[Path] = []
    expect_value = False
    for arg in args:
        if expect_value:
            expect_value = False
            continue
        if arg.startswith("-"):
            expect_value = arg in _VALUE_FLAGS
            continue
        selected.append(Path(arg))
    if not selected:
        selected = [Path("tests")]

    # A root-level conftest.py applies to all collected tests. For a named test
    # file, its containing directory is the smallest collection root that can
    # contain a local conftest.py.
    # The current working directory contributes only its direct conftest.py / config.
    # A sibling subsystem (for example mission_control/tests/) must not block a
    # builder-only collection merely because it has its own real-pytest setup.
    cwd = Path.cwd().resolve()
    direct_root_conftest = cwd / "conftest.py"
    if direct_root_conftest.is_file():
        return direct_root_conftest
    root_pyproject = cwd / "pyproject.toml"
    if root_pyproject.is_file():
        try:
            content = root_pyproject.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if any(line.strip() == "[tool.pytest.ini_options]" for line in content.splitlines()):
            return root_pyproject

    roots = [path if path.is_dir() else path.parent for path in selected]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        direct = root / "conftest.py"
        if direct.is_file():
            return direct
        if root.is_dir():
            found = next(iter(sorted(root.rglob("conftest.py"))), None)
            if found is not None:
                return found

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                content = pyproject.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(line.strip() == "[tool.pytest.ini_options]" for line in content.splitlines()):
                return pyproject

        # Real pytest applies configuration from ancestors of an explicitly named
        # leaf test too. Inspect them up to the invocation root so a command such
        # as `pytest tests/unit/test_x.py` cannot bypass tests/conftest.py.
        parent = root.parent
        while parent != parent.parent:
            conftest = parent / "conftest.py"
            if conftest.is_file():
                return conftest
            parent_pyproject = parent / "pyproject.toml"
            if parent_pyproject.is_file():
                try:
                    content = parent_pyproject.read_text(encoding="utf-8")
                except OSError:
                    content = ""
                if any(line.strip() == "[tool.pytest.ini_options]" for line in content.splitlines()):
                    return parent_pyproject
            if parent == cwd:
                break
            parent = parent.parent
    return None


def _load(path: Path):
    name = "pytest_local_" + "_".join(path.with_suffix("").parts)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run_test(func) -> tuple[bool, bool, str]:
    sig = inspect.signature(func)
    kwargs = {}
    tmpdirs: list[tempfile.TemporaryDirectory[str]] = []
    for name in sig.parameters:
        if name == "tmp_path":
            tmp = tempfile.TemporaryDirectory()
            tmpdirs.append(tmp)
            kwargs[name] = Path(tmp.name)
        else:
            return False, False, f"unsupported fixture {name}"
    try:
        func(**kwargs)
        return True, False, ""
    except SkipTest as exc:
        return True, True, str(exc)
    except Exception:
        return False, False, traceback.format_exc()
    finally:
        for tmp in tmpdirs:
            tmp.cleanup()


# A configured memory provider's credentials must never reach a test.
#
# `_dispatch_runtime/memory_hook.py:_hive_client()` builds a REAL client whenever
# HIVEMIND_MCP_URL + HIVEMIND_API_KEY are merely PRESENT in the environment, and
# `lane_presence` / `run_ledger` then talk to whatever that points at. Nothing in the code
# distinguishes a unit test from a real dispatch, so on any machine where those vars are
# exported — a developer's shell, a CI runner — the suite silently writes fixture data to a
# live service and burns real quota. Metered APIs charge for failed calls too, so a suite
# that merely ERRORS against production still costs.
#
# This guard lives HERE, not in a conftest.py: this shim is the repo's actual test
# runner (`python3 -m pytest` resolves to it from the repo root) and it implements no
# conftest, no fixtures and no autouse — only `tmp_path` (see _run_test). A conftest.py
# would be silently ignored, which is exactly the kind of guard-that-does-nothing this
# incident was made of.
#
# Tests that legitimately exercise the configured path set these vars themselves, at
# test time, and are unaffected. Reaching the network must be an explicit local act,
# never an ambient inheritance.
_HIVE_ENV = ("HIVEMIND_MCP_URL", "HIVEMIND_API_KEY", "HIVEMIND_TIMEOUT_MS")


def _isolate_hive_env() -> None:
    for var in _HIVE_ENV:
        os.environ.pop(var, None)


# Outcome capture for the guard-outcomes check (scripts/_validators/check_guard_outcomes.py):
# the AST-based SSOT drift check (behaviors.py) can only see a guarding test's *source* —
# a decorator or a commented-out def. It cannot see a runtime `raise SkipTest` (or any other
# conditional skip) inside the test body, so a guarding test that quietly skips every time the
# real gate runs still counts as a "live guard" to that check. Opt-in via env so this shim's
# default behavior (and output) is byte-for-byte unchanged when the variable is unset.
def _write_outcomes(ran: list[str], skipped: list[str]) -> None:
    target = os.environ.get("PYTEST_SHIM_OUTCOMES")
    if not target:
        return
    payload = {"ran": ran, "skipped": skipped}
    Path(target).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    _isolate_hive_env()
    try:
        keyword_expression = _keyword_expression(sys.argv[1:])
    except ValueError as exc:
        print(f"pytest: error: {exc}", file=sys.stderr)
        return 4
    unsupported = _unsupported_pytest_setup(sys.argv[1:])
    if unsupported is not None:
        if unsupported.name == "conftest.py":
            print(
                f"pytest shim: refuses to ignore unsupported conftest.py: {unsupported}\n"
                "This runner does not implement conftest; put the logic in pytest/__main__.py, "
                "or run real pytest from outside this repo root (installing it is not enough -- "
                "the `pytest/` package here shadows it).",
                file=sys.stderr,
            )
        else:
            print(
                f"pytest shim: refuses to ignore [tool.pytest.ini_options] in {unsupported}\n"
                "This runner does not implement pytest configuration; put the logic in "
                "pytest/__main__.py or install real pytest.",
                file=sys.stderr,
            )
        return 4
    failures: list[str] = []
    skipped: list[str] = []
    ran_ids: list[str] = []
    skipped_ids: list[str] = []
    count = 0
    for path in _paths(sys.argv[1:]):
        module = _load(path)
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            obj = getattr(module, name)
            if callable(obj):
                if not _matches_keyword(keyword_expression, f"{path}::{name}"):
                    continue
                node_id = f"{path}::{name}"
                count += 1
                ok, was_skipped, message = _run_test(obj)
                ran_ids.append(node_id)
                if was_skipped:
                    skipped.append(f"{node_id}: {message}")
                    skipped_ids.append(node_id)
                if not ok:
                    failures.append(f"{node_id}\n{message}")
    _write_outcomes(ran_ids, skipped_ids)
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} failed, {count - len(failures) - len(skipped)} passed, {len(skipped)} skipped")
        return 1
    if count == 0:
        print("no tests ran")
        return 5
    for skip in skipped:
        print(f"SKIPPED {skip}")
    print(f"{count - len(skipped)} passed" + (f", {len(skipped)} skipped" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
