#!/usr/bin/env python3
"""Pre-publish scrub gate — mechanical, and it fails the build.

Nothing publishes before this passes. A one-time human read of the tree is a scrub you can never
*prove* complete; one missed blob is permanent. So this is a GATE, not a checklist: it scans the
publishable set for secrets and personal data and exits non-zero on any hit.

Two layers, because they answer different questions:
  * EXCLUDES — paths that must never leave the development tree at all: a maintainer's own
    specs and queue state, internal planning notes, and local tooling. The export drops these.
  * PATTERNS — secrets, PII, and private-fleet coupling that must not appear in what DOES publish,
    even by accident. High-signal only, so a green result means something. A line may be cleared
    with an inline `# publish-ok: <reason>` marker
    (use sparingly, and only for a genuine placeholder/example).

Usage:
    pre-publish-scan.py [--root .] [--all]        # scan the publishable set (default) or everything
    pre-publish-scan.py --list-publishable        # print what WOULD publish, then exit 0
Exit: 0 clean · 1 findings · 2 usage/error.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import types
from pathlib import Path
from _dispatch_runtime.paths import RUNTIME_DIR_NAMES

# ---- what must NEVER publish (dropped by the export; also skipped by the scan) -----------------
# Glob-ish prefixes matched against the repo-relative POSIX path.
DEFAULT_EXCLUDES = (
    *(f"{name}/" for name in RUNTIME_DIR_NAMES),  # a maintainer's own specs + queue state
    ".tg-bridge/",
    ".mission-control/", ".hive-claude/", ".claude/", "memory/",
    "docs/mission-control-architecture.md",  # a retired surface, kept for history, never shipped
    "e2e/",                   # browser-test stack for a retired surface
    "docs/planning/",           # internal planning + design docs (quote secrets as examples)
    "docs/archive/",
    "docs/vscode-attach.md",
    "docs/PUBLISH.md",         # internal publish runbook
    # Documents a subsystem that was RETIRED, not shipped: every path it names
    # (builder_intents/, api/intents.py, web/intent-ui/, scripts/run-intent-ui.sh) is gone, and
    # tests/unit/test_intent_file_native.py asserts the intent layer must not reuse them. A
    # public reader following its "Run it" section fails on the first command.
    "docs/intent-workspace-ui.md",
    "scripts/_scrub_private.txt",  # local denylist, never exported
    "scripts/builder-group-runner.py",
    ".codex-", ".git/",
)

# ---- secrets: high-confidence key shapes (word-bounded so `risk-sensitive` is not a hit) --------
SECRET_PATTERNS = {
    "openai/anthropic key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github token": re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    "github fine-grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "hivemind key": re.compile(r"\bhive_[A-Za-z0-9_-]{20,}"),
    "slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    # Quoted OR bare. It used to require quotes, which meant a tracked `.env` -- where nothing is
    # quoted -- passed the gate entirely: AWS_SECRET_ACCESS_KEY=..., DB_PASSWORD=..., API_KEY=...
    # all sailed through. SECURITY.md invites third parties to rely on this scanner for their own
    # publication, so the rule has to cover the shape secrets are actually written in.
    # Two shapes, because secrets are written two ways and the old rule only knew one.
    #
    #   QUOTED   `password = "hunter2hunter2"`  -- source code. Any spacing.  # publish-ok: rule doc
    #   UNQUOTED `DB_PASSWORD=hunter2hunter2`   -- a .env file, which quotes nothing.
    #
    # The unquoted branch requires `=` with NO surrounding whitespace, and a value made only of
    # credential characters. That is what separates an env assignment from Python: PEP 8 puts
    # spaces around `=`, and an expression like `api_key = os.environ.get("X")` contains
    # parentheses and quotes. Without that discriminator the unquoted branch fired on 25 ordinary
    # assignments in this tree, and a gate that cries wolf gets bypassed.
    #
    # The keyword may sit INSIDE a compound name -- `\b` was the third bug here, because `_` is a
    # word character, so `DB_PASSWORD` and `AWS_SECRET_ACCESS_KEY` never matched at all.
    # The two branches take DIFFERENT name shapes, and that asymmetry is the point:
    #   * source code writes a bare keyword (`password = "..."`), so `\b` there keeps
    #     `_SUBSCRIPTION_TOKEN_FILE_ENV = "..."` and `CLAUDE_CODE_API_KEY="dummy"` -- both of which
    #     hold an env var NAME, not a secret -- from firing;
    #   * a .env file writes compound names (`DB_PASSWORD=`, `AWS_SECRET_ACCESS_KEY=`), which is
    #     exactly where `\b` failed, since `_` is a word character.
    "generic secret assignment": re.compile(
        r"(?i)(?:\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
        r"|[A-Za-z0-9_-]*(?:api[_-]?key|secret|password|passwd|token)[A-Za-z0-9_-]*"
        r"=[A-Za-z0-9_\-./+~]{12,}(?:\s|$))"),
}

# ---- public, generic personal-data shapes ---------------------------------------------------------
PII_PATTERNS = {
    "telegram chat id": re.compile(r"-100\d{9,}"),
    "email address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "user home path": re.compile(r"/(?:Users|home)/[^/\s]+"),
}

# ---- private fleet coupling: public code must not depend on the author's infrastructure --------
FLEET_PATTERNS = {
    # A workspace root is a private-fleet assumption until explicitly reviewed.  # publish-ok: scanner pattern
    "private fleet path": re.compile(
        # The exemptions are DOC PLACEHOLDERS only. `\.` used to sit in this lookahead and
        # exempted every dot-prefixed path, so a real dot-named worktree of a private repo
        # shipped inside standards/builder-contract.md — a file the installer
        # copies into every user's project. A leading dot is not a placeholder; it is usually a
        # runtime dir, which is exactly the thing that must not leak.
        r"/workspaces/(?!example(?:/|$)|tmp(?:/|$)|repo(?:/|$)|<repo>)[A-Za-z0-9._-]+"
    ),  # publish-ok: scanner pattern
    "private lab path": re.compile(r"/l[a]b/"),  # publish-ok: scanner pattern
    "private hermes path": re.compile(r"/opt-herm[e]s\b"),  # publish-ok: scanner pattern
    "private watchdog": re.compile(r"\bbuilder-daemon-watchdog\b"),  # publish-ok: scanner pattern
}

ALLOW_MARKER = re.compile(r"#\s*publish-ok:")
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".html", ".css",
                 ".js", ".ts", ".sh", ".cfg", ".ini", ".env", ".example", ".svg",
                 ".webmanifest", ""}
MANDATORY_SECRET_SUFFIXES = {".pem", ".key", ".crt", ".p12", ".der"}


def private_denylist_patterns(root: Path) -> dict[str, re.Pattern[str]]:
    """Load non-exported exact literals without embedding them in public source."""
    path = root / "scripts" / "_scrub_private.txt"
    if not path.is_file():
        return {}
    try:
        entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    except OSError as exc:
        raise RuntimeError(f"cannot read private scrub denylist: {exc}") from exc
    return {"private denylist": re.compile("|".join(re.escape(entry) for entry in entries))} if entries else {}


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
    if out.returncode != 0:
        detail = out.stderr.strip() or "git ls-files failed"
        raise RuntimeError(detail)
    return [line for line in out.stdout.splitlines() if line]


def is_excluded(rel: str, excludes: tuple[str, ...]) -> bool:
    return any(rel.startswith(e) if e.endswith("/") else rel == e for e in excludes)


def publishable(root: Path, excludes: tuple[str, ...]) -> list[str]:
    return [f for f in tracked_files(root) if not is_excluded(f, excludes)]


def is_text_asset(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in TEXT_SUFFIXES or path.suffix.lower() in MANDATORY_SECRET_SUFFIXES or name.startswith(".env.")


def _read_text_asset(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, str(exc)


def _fold_string(node: ast.AST) -> str | None:
    """Return a value only for pure, string-valued Python expressions.

    This deliberately does not resolve names or execute arbitrary calls.  The
    sole call form accepted is a constant separator's ``str.join`` over a
    literal tuple/list of recursively constant strings.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold_string(node.left), _fold_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        values: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                values.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                value = _fold_string(part.value)
                if value is None or part.format_spec is not None:
                    return None
                if part.conversion == -1 or part.conversion == ord("s"):
                    values.append(value)
                elif part.conversion == ord("r"):
                    values.append(repr(value))
                elif part.conversion == ord("a"):
                    values.append(ascii(value))
                else:
                    return None
            else:
                return None
        return "".join(values)
    if (isinstance(node, ast.Call) and not node.keywords and len(node.args) == 1
            and isinstance(node.func, ast.Attribute) and node.func.attr == "join"):
        separator = _fold_string(node.func.value)
        sequence = node.args[0]
        if separator is None or not isinstance(sequence, (ast.Tuple, ast.List)):
            return None
        values = [_fold_string(element) for element in sequence.elts]
        return separator.join(values) if all(value is not None for value in values) else None
    return None


def _compiled_string_constants(code: types.CodeType) -> list[str]:
    """Return text represented by recursively nested immutable constant pools.

    CPython folds more pure literal expressions than our intentionally narrow AST
    evaluator (for example repetition and constant subscripting).  It does not
    fold runtime calls such as ``str.join``, which remain covered by
    ``_fold_string`` above.  This still deliberately does not resolve runtime
    values: ``chr``, decoding/base64, name concatenation, and formatting with
    name-derived arguments are outside this mechanical gate's scope.
    """
    values: list[str] = []
    seen: set[int] = set()

    def walk(value: object) -> None:
        # Constants are normally acyclic, but use object identity defensively:
        # unusual code objects or container subclasses must not make this gate
        # recurse forever.
        if isinstance(value, str):
            values.append(value)
            return
        if isinstance(value, (bytes, bytearray)):
            # This inspects an already-compiled literal only; it never evaluates
            # decode/base64 calls.  latin-1 is total and preserves byte positions,
            # which makes it a safe boundary for pattern matching.
            values.append(bytes(value).decode("latin-1"))
            return
        if not isinstance(value, (types.CodeType, tuple, frozenset)):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, types.CodeType):
            children = value.co_consts
        else:
            children = value
        for child in children:
            walk(child)

    walk(code)
    return values


def _pattern_findings(rel: str, line: int, value: str, raw_line: str,
                      private_patterns: dict[str, re.Pattern[str]]) -> set[tuple[str, int, str, str]]:
    if ALLOW_MARKER.search(raw_line):
        return set()
    return {
        (rel, line, name, match.group(0))
        for patterns in (SECRET_PATTERNS, PII_PATTERNS, FLEET_PATTERNS, private_patterns)
        for name, rx in patterns.items()
        for match in rx.finditer(value)
    }


def scan_file(root: Path, rel: str, private_patterns: dict[str, re.Pattern[str]] | None = None) -> list[tuple[str, int, str, str]]:
    findings: list[tuple[str, int, str, str]] = []
    path = root / rel
    private_patterns = private_patterns if private_patterns is not None else private_denylist_patterns(root)
    if not is_text_asset(path):
        return findings
    text, error = _read_text_asset(path)
    if error is not None:
        return [(rel, 0, "unreadable text asset", error)]
    assert text is not None

    seen: set[tuple[str, int, str, str]] = set()
    lines = text.splitlines()
    for index, raw_line in enumerate(lines, start=1):
        seen.update(_pattern_findings(rel, index, raw_line, raw_line, private_patterns))
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            return sorted(seen | {(rel, exc.lineno or 0, "unparseable python asset", exc.msg)})
        # Map AST-folded values to their source line.  This both keeps findings
        # readable and lets an inline publish-ok marker suppress an equivalent
        # compiler-pool hit (notably the scanner's own documented test pattern).
        folded_sources: dict[str, tuple[int, str]] = {}
        for node in ast.walk(tree):
            value = _fold_string(node)
            if not hasattr(node, "lineno"):
                continue
            line = node.lineno
            raw_line = lines[line - 1] if line <= len(lines) else ""
            if value is not None:
                folded_sources.setdefault(value, (line, raw_line))
                seen.update(_pattern_findings(rel, line, value, raw_line, private_patterns))
            # A marker on a source expression must also clear a value that only
            # the compiler folds (the test module deliberately builds secret
            # shapes this way). Compile that expression alone to associate its
            # constant-pool values with the annotated raw line.
            if ALLOW_MARKER.search(raw_line):
                try:
                    expression = ast.fix_missing_locations(ast.Expression(body=node))
                    for constant in _compiled_string_constants(compile(expression, rel, "eval")):
                        folded_sources.setdefault(constant, (line, raw_line))
                except (SyntaxError, TypeError, ValueError):
                    pass
        # Scan CPython's recursively nested constant pools too.  They cover
        # compiler-folded literal forms the hand-rolled AST folder deliberately
        # does not enumerate, while the AST mapping preserves a line when one
        # is determinable.
        try:
            compiled = compile(text, rel, "exec")
        except SyntaxError as exc:  # Defensive: AST parsing above already fails closed.
            return sorted(seen | {(rel, exc.lineno or 0, "unparseable python asset", exc.msg)})
        for value in _compiled_string_constants(compiled):
            line, raw_line = folded_sources.get(value, (0, ""))
            seen.update(_pattern_findings(rel, line, value, raw_line, private_patterns))
    return sorted(seen)


def scan(root: Path, files: list[str]) -> list[tuple[str, int, str, str]]:
    private_patterns = private_denylist_patterns(root)
    out: list[tuple[str, int, str, str]] = []
    for rel in files:
        out.extend(scan_file(root, rel, private_patterns))
    return out


def _redact(hit: str) -> str:
    return hit if len(hit) <= 10 else f"{hit[:6]}…{hit[-3:]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pre-publish scrub gate")
    ap.add_argument("--root", default=".")
    ap.add_argument("--all", action="store_true",
                    help="scan the whole tracked tree, not just the publishable set")
    ap.add_argument("--list-publishable", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        all_tracked = tracked_files(root)
    except RuntimeError as exc:
        print(f"pre-publish scan ERROR — cannot list tracked files: {exc}", file=sys.stderr)
        return 2
    pub = [f for f in all_tracked if not is_excluded(f, DEFAULT_EXCLUDES)]
    if not pub:
        print("pre-publish scan ERROR — publishable set is empty", file=sys.stderr)
        return 2
    if args.list_publishable:
        for f in pub:
            print(f)
        excluded = len(all_tracked) - len(pub)
        print(f"\n{len(pub)} publishable · {excluded} excluded (never leave the private tree)",
              file=sys.stderr)
        return 0

    target = all_tracked if args.all else pub
    inspected = 0
    errors = 0
    for rel in target:
        path = root / rel
        if is_text_asset(path):
            text, error = _read_text_asset(path)
            if error is None:
                inspected += 1
            else:
                errors += 1
    skipped = len(target) - inspected - errors
    findings = scan(root, target)
    if not findings:
        print(f"pre-publish scan CLEAN — {inspected} text file(s) inspected; {skipped} binary/unknown "
              f"file(s) skipped; {errors} unreadable text asset(s){' (whole tree)' if args.all else ' (publishable set)'}.")
        return 0

    by_cat: dict[str, list] = {}
    for rel, n, name, hit in findings:
        by_cat.setdefault(name, []).append((rel, n, hit))
    print(f"pre-publish scan FOUND {len(findings)} sensitive match(es). Publishing is BLOCKED.\n",
          file=sys.stderr)
    for name in sorted(by_cat):
        print(f"  [{name}]  {len(by_cat[name])} hit(s)", file=sys.stderr)
        for rel, n, hit in by_cat[name][:20]:
            print(f"    {rel}:{n}   {_redact(hit)}", file=sys.stderr)
        if len(by_cat[name]) > 20:
            print(f"    … and {len(by_cat[name]) - 20} more", file=sys.stderr)
    print("\nRemove them, move the file behind an EXCLUDE, or clear a genuine placeholder line with "
          "`# publish-ok: <reason>`.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
