"""Drift check for the behavioral SSOT (docs/system-behaviors.yaml).

Every documented behavior must name at least one guarding test that (a) exists and (b) is actually run
by `make gate`. A behavior with no live, gated test is a claim the host cannot verify — the exact drift
this refuses. Run in the gate via tests/unit/test_system_behaviors.py. Stdlib-only, deterministic.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REQUIRED_KEYS = {"id", "area", "behavior", "invariant", "guarding_tests", "breaks_when"}

_SKIP_DECORATOR_NAMES = {"skip", "skipif", "xfail"}


def _decorator_name(dec: ast.expr) -> str | None:
    """Best-effort dotted-name (or call target) of a decorator, e.g. 'pytest.mark.skip' or 'skip'."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return None
    return ".".join(reversed(parts))


def _is_skip_decorator(dec: ast.expr) -> bool:
    name = _decorator_name(dec)
    if not name:
        return False
    tail = name.rsplit(".", 1)[-1]
    return tail in _SKIP_DECORATOR_NAMES or name in {"unittest.skip", "unittest.skipIf"}


def _parse_module(path: Path, cache: dict[Path, ast.Module | None]) -> ast.Module | None:
    if path in cache:
        return cache[path]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, ValueError, OSError):
        tree = None
    cache[path] = tree
    return tree


def _find_test_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """A module-level (not nested/commented-out) function or async function named `name`."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


_TS_JS_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".mts"}

# `it`/`test`/`xit`/`xtest`, with an optional `.skip`/`.todo`/`.only` modifier, then the
# quoted title. Requires the FULL quoted string to close before comparing (never a substring
# match), and tolerates escaped quotes inside the title.
_TEST_CALL_RE = re.compile(
    r"\b(it|test|xit|xtest)(\.(?:skip|todo|only))?\s*\(\s*(['\"`])((?:\\.|(?!\3).)*)\3"
)


def _unescape_js_string(raw: str) -> str:
    """Undo simple JS/TS string escapes (\\', \\", \\`, \\\\, ...) well enough to compare a
    quoted literal against a plain title. Textual, not a real JS-string-literal parser."""
    return re.sub(r"\\(.)", r"\1", raw)


def _describe_skip_spans(text: str) -> list[tuple[int, int]]:
    """Byte-offset spans of every `describe.skip(...) => { ... }` callback body, found with a
    naive brace counter. No TS parser is available (stdlib-only, per this module's docstring);
    this is deliberately textual, not a real parse."""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\bdescribe\.skip\s*\(", text):
        brace_start = text.find("{", m.end())
        if brace_start == -1:
            continue
        depth = 1
        i = brace_start + 1
        n = len(text)
        while i < n and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        spans.append((brace_start, i))
    return spans


def _find_ts_test(path: Path, name: str, cache: dict[Path, str | None]) -> str:
    """Textually resolve a TS/JS guarding_test title. Returns one of:
    'live' (a real, un-skipped `it`/`test`), 'skip' (skip/todo/xit/xtest/enclosing
    describe.skip), 'only' (`.only`-marked -- suppresses the rest of the file), or 'missing'
    (title not found, or the file could not be read)."""
    if path not in cache:
        try:
            cache[path] = path.read_text(encoding="utf-8")
        except OSError:
            cache[path] = None
    text = cache[path]
    if text is None:
        return "missing"

    skip_spans = _describe_skip_spans(text)

    def _in_skip_block(pos: int) -> bool:
        return any(start <= pos < end for start, end in skip_spans)

    found_skip = found_only = found_live = False
    for m in _TEST_CALL_RE.finditer(text):
        base, modifier, raw = m.group(1), m.group(2), m.group(4)
        if _unescape_js_string(raw) != name:
            continue
        if base in ("xit", "xtest") or modifier in (".skip", ".todo") or _in_skip_block(m.start()):
            found_skip = True
        elif modifier == ".only":
            found_only = True
        else:
            found_live = True

    if found_skip:
        return "skip"
    if found_only:
        return "only"
    if found_live:
        return "live"
    return "missing"


def _load_yaml(path: Path):
    from _yaml import yaml  # repo ships a shim when PyYAML is absent; the shape we read is simple lists/strings
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_RUNNER_WHOLESALE_EXTS = {
    "vitest": {".ts", ".tsx", ".js", ".mjs", ".mts"},
    "jest": {".ts", ".tsx", ".js", ".mjs", ".mts"},
    "mocha": {".ts", ".tsx", ".js", ".mjs", ".mts"},
    "pytest": {".py"},
}

# A known runner invocation, optionally via `npx`/`pnpm [exec]`/`yarn [exec]`, optionally
# followed by a `run` subcommand (vitest/jest convention), then whatever args follow on that
# same command (up to a shell separator or newline). Anchored to line-start-or-shell-separator
# (allowing leading whitespace, e.g. a Makefile recipe's tab indent) so it does not fire mid-token
# (e.g. the "pytest" inside "python3 -m pytest ..." is intentionally NOT matched here -- that
# invocation already gets its explicit-path coverage from the existing tests/scripts/ token scan).
_RUNNER_INVOCATION_RE = re.compile(
    r"(?:^\s*|[;&|]\s*)(?:npx\s+|pnpm\s+(?:exec\s+)?|yarn\s+(?:exec\s+)?)?"
    r"(vitest|jest|mocha|pytest)\b(?:\s+run)?([^\n;&|]*)",
    re.MULTILINE,
)

_EXPLICIT_TS_PATH_RE = re.compile(r"[\w./-]+\.(?:ts|tsx|js|mjs|mts)\b")

# `pnpm test` / `npm test` / `pnpm -s test` / `pnpm run test` / `npm run test` -- one level of
# package-script indirection, resolved by reading package.json's scripts.test.
_PKG_TEST_SCRIPT_RE = re.compile(r"\b(?:pnpm|npm|yarn)\s+(?:-s\s+)?(?:run\s+)?test\b")


def _expand_package_script_indirection(root: Path, block: str) -> str:
    """If the gate block invokes `pnpm test`/`npm test`/etc, resolve ONE level of indirection by
    reading package.json's scripts.test and appending it for runner analysis. Never recurses."""
    if not _PKG_TEST_SCRIPT_RE.search(block):
        return block
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return block
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return block
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test_script, str):
        return block
    return block + "\n" + test_script


def _scan_runner_invocations(block: str) -> tuple[set[str], set[str]]:
    """Explicit TS/JS file tokens + wholesale extensions contributed by known-runner invocations
    in a gate command block. A runner invoked with no non-flag args discovers its whole
    conventional set (wholesale); a runner invoked WITH path args covers only those paths."""
    explicit_files: set[str] = set()
    wholesale_exts: set[str] = set()
    for m in _RUNNER_INVOCATION_RE.finditer(block):
        runner, rest = m.group(1), m.group(2).strip()
        tokens = [t for t in rest.split() if t and not t.startswith("-")]
        if tokens:
            for t in tokens:
                fm = _EXPLICIT_TS_PATH_RE.search(t)
                if fm:
                    explicit_files.add(fm.group(0))
        else:
            wholesale_exts |= _RUNNER_WHOLESALE_EXTS.get(runner, set())
    return explicit_files, wholesale_exts


def _setup_decisions_gate_block(root: Path) -> str:
    """The armed host_verify gate command(s) for a repo with no Makefile: builder's own
    definition of 'what the gate runs' is .builder/setup-decisions.yaml's commands.default
    (test, plus check if present). Never raises -- a repo with no such file has no gate at all,
    which is handled by the caller returning empty coverage, not by crashing here."""
    sd_path = root / ".builder" / "setup-decisions.yaml"
    if not sd_path.is_file():
        return ""
    try:
        data = _load_yaml(sd_path)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    default = data.get("commands")
    default = default.get("default") if isinstance(default, dict) else None
    if not isinstance(default, dict):
        return ""
    parts = [default[k] for k in ("test", "check") if isinstance(default.get(k), str)]
    return "\n".join(parts)


def _gate_covered(root: Path) -> tuple[set[str], set[str], set[str]]:
    """The test files + wholesale dirs/extensions the gate runs.

    Makefile `gate:` recipe if present (today's behavior, unchanged); else the repo's own armed
    setup-decisions.yaml commands (still 'what the gate runs', just not a Makefile); if neither
    exists, nothing is covered -- every behavior then honestly reports 'not run by the gate'
    rather than raising.
    """
    block = ""
    mk_path = root / "Makefile"
    if mk_path.is_file():
        mk = mk_path.read_text(encoding="utf-8")
        m = re.search(r"^gate:.*?\n((?:\t.*\n)+)", mk, re.M | re.S)
        if m:
            block = m.group(1)
    if not block:
        block = _setup_decisions_gate_block(root)
    block = _expand_package_script_indirection(root, block)
    # Classify each pytest path arg the gate runs: a `.py` token is an explicit file,
    # any other tests/ or scripts/ path token is a wholesale directory (running it covers
    # every file beneath it). This must stay filename-agnostic so a directory-based gate
    # can never silently drop a new test — the exact drift this check exists to refuse.
    tokens = set(re.findall(r"(?:tests|scripts)/[\w./-]+", block))
    files = {t for t in tokens if t.endswith(".py")}
    dirs = {t for t in tokens if not t.endswith(".py")}
    explicit_ts_files, wholesale_exts = _scan_runner_invocations(block)
    files |= explicit_ts_files
    return files, dirs, wholesale_exts


def _covered(path_s: str, files: set[str], dirs: set[str], wholesale_exts: frozenset[str] = frozenset()) -> bool:
    if path_s in files or any(path_s.startswith(d + "/") for d in dirs):
        return True
    return bool(wholesale_exts) and Path(path_s).suffix in wholesale_exts


def _normalize_ref(ref):
    """A guarding_test ref is `PATH::name`. Real PyYAML reads it as a string; the repo's lossy shim
    splits on the first colon and returns `{PATH: ':name'}` — reconstruct that back to `PATH::name`."""
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict) and len(ref) == 1:
        key, val = next(iter(ref.items()))
        val = "" if val is None else str(val)
        # TWO different producers land here, and they need opposite reconstructions:
        #  (a) the lossy YAML shim splits 'PATH::name' into {PATH: ':name'} -- val KEEPS the colon,
        #      so PATH + val restores it exactly.
        #  (b) real PyYAML parsing an UNQUOTED ref whose test title contains ': ' reads it as a
        #      mapping: 'p.test.ts::single-low: $12' -> {'p.test.ts::single-low': '$12'}. Here the
        #      ': ' separator was CONSUMED, so it must be put back or the title silently loses a
        #      space and the guard is reported "not defined" even though it exists. TS test titles
        #      commonly contain colons, so this is the normal case, not an edge case.
        return f"{key}:{val}" if val.startswith(":") else f"{key}: {val}"
    return None


def _anchor_findings(root: Path, bid: str, raw_anchors) -> list[str]:
    """Verify a behavior's optional `anchors` still pin real text in real files.

    An anchor records WHERE a load-bearing literal lives, e.g.
        anchors:
          - path: packages/product-model/src/bands.ts
            contains: "priceCents: 1200,"

    The key is optional -- repos that do not use it are unaffected. But when it IS present it must
    be load-bearing, not decorative: an anchor nobody checks is a comment that looks like a guard.
    Each `contains` must include the VALUE (`"FREE_REFINES = 5"`, not `"FREE_REFINES"`), otherwise
    it cannot go red when the value changes, which is the only thing an anchor is for.
    """
    findings: list[str] = []
    if raw_anchors is None:
        return findings
    if not isinstance(raw_anchors, list):
        return [f"{bid}: anchors must be a list"]
    for i, anchor in enumerate(raw_anchors):
        if not isinstance(anchor, dict):
            findings.append(f"{bid}: anchor #{i} is not a mapping")
            continue
        rel = str(anchor.get("path") or "").strip()
        needle = anchor.get("contains")
        if not rel or not isinstance(needle, str) or not needle:
            findings.append(f"{bid}: anchor #{i} needs both `path` and a non-empty `contains`")
            continue
        target = root / rel
        if not target.is_file():
            findings.append(f"{bid}: anchor file not found: {rel}")
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # noqa: BLE001
            findings.append(f"{bid}: anchor file unreadable: {rel} ({exc})")
            continue
        if needle not in text:
            findings.append(
                f"{bid}: anchor no longer present in {rel}: {needle!r} "
                "-- the code moved and the SSOT still claims it")
    return findings


def check_behavior_drift(root: Path) -> list[str]:
    """Return human-readable drift findings; empty list means the SSOT is honest."""
    root = Path(root)
    doc = root / "docs" / "system-behaviors.yaml"
    if not doc.is_file():
        return [f"missing behavioral SSOT: {doc}"]
    try:
        data = _load_yaml(doc)
    except Exception as exc:  # noqa: BLE001 — a doc we cannot parse is drift, not a traceback
        return [f"system-behaviors.yaml: could not parse ({exc})"]

    findings: list[str] = []
    if not isinstance(data, dict) or data.get("schema") != "system-behaviors/v1":
        findings.append("system-behaviors.yaml: schema must be 'system-behaviors/v1'")
    behaviors = (data or {}).get("behaviors") if isinstance(data, dict) else None
    if not isinstance(behaviors, list) or not behaviors:
        return findings + ["system-behaviors.yaml: no behaviors defined"]

    files, dirs, wholesale_exts = _gate_covered(root)
    ast_cache: dict[Path, ast.Module | None] = {}
    ts_cache: dict[Path, str | None] = {}
    seen: set[str] = set()
    for i, b in enumerate(behaviors):
        if not isinstance(b, dict):
            findings.append(f"behavior #{i}: not a mapping")
            continue
        bid = str(b.get("id") or f"#{i}")
        missing = REQUIRED_KEYS - set(b)
        if missing:
            findings.append(f"{bid}: missing keys {sorted(missing)}")
        if bid in seen:
            findings.append(f"{bid}: duplicate id")
        seen.add(bid)
        findings.extend(_anchor_findings(root, bid, b.get("anchors")))
        gts = b.get("guarding_tests")
        if not isinstance(gts, list) or not gts:
            findings.append(f"{bid}: no guarding_tests")
            continue
        for raw in gts:
            ref = _normalize_ref(raw)
            if not ref or "::" not in ref:
                findings.append(f"{bid}: malformed guarding_test ref {raw!r}")
                continue
            path_s, name = ref.split("::", 1)
            tp = root / path_s
            if not tp.is_file():
                findings.append(f"{bid}: guarding_test file not found: {path_s}")
                continue
            if tp.suffix in _TS_JS_SUFFIXES:
                status = _find_ts_test(tp, name, ts_cache)
                if status == "missing":
                    findings.append(f"{bid}: test '{name}' not defined in {path_s}")
                    continue
                if status == "skip":
                    findings.append(f"{bid}: test '{name}' is skip/xfail-marked — not a live guard")
                    continue
                if status == "only":
                    findings.append(f"{bid}: test '{name}' is .only-marked -- it suppresses the rest of the file")
                    continue
            else:
                tree = _parse_module(tp, ast_cache)
                if tree is None:
                    findings.append(f"{bid}: test '{name}' not defined in {path_s}")
                    continue
                func = _find_test_function(tree, name)
                if func is None:
                    findings.append(f"{bid}: test '{name}' not defined in {path_s}")
                    continue
                if any(_is_skip_decorator(d) for d in func.decorator_list):
                    findings.append(f"{bid}: test '{name}' is skip/xfail-marked — not a live guard")
                    continue
            if not _covered(path_s, files, dirs, wholesale_exts):
                findings.append(f"{bid}: {path_s} is not run by `make gate` (behavior would go unverified in CI)")

    gaps = (data or {}).get("gaps") if isinstance(data, dict) else None
    if isinstance(gaps, list):
        for g in gaps:
            gid = str(g.get("id")) if isinstance(g, dict) else None
            if gid and gid in seen:
                findings.append(f"gaps: id '{gid}' collides with a documented behavior id — a gap cannot also claim to be verified")
    return findings
