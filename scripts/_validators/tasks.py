from __future__ import annotations

import os
import re
import sys
from typing import Any

from .canonical import validate_canonical_artifact
from .common import VALID_TDD_EXEMPT_REASONS, ValidationContext, string_list
from .renderers import render_tasks


def _verify_lint_enforced() -> bool:
    """The verify-command denylist (P0-1b) is staged like BUILDER_TRACE_COVERAGE:
    `warn` (default) records advisories to stderr and never blocks; `enforce` promotes
    them to hard errors. Flip to enforce only after existing specs are audited (operator
    sentinels, diagnostic commands) — see the review corpus scan."""
    return (os.environ.get("BUILDER_VERIFY_LINT", "warn") or "warn").strip().lower() == "enforce"


# The live host-verify gate judges a verify command by its EXIT CODE ONLY —
# stdout/stderr and `#` comments are discarded. These markers signal that a bare
# `grep` reads as a zero-hits (absence) assertion, which inverts under that gate:
# `grep` exits 1 on zero matches, so it FAILS exactly when a deletion/refactor
# succeeded. Such a grep must be negated (`! grep …`). A bare PRESENCE grep
# (exit 0 when the string is found) is probative and left alone.
_ABSENCE_MARKERS = (
    "zero hit",
    "no hit",
    "0 hit",
    "no match",
    "not found",
    "not present",
    "must not",
    "should not",
    "no longer",
    "absent",
)


# A verify item (or task) MAY declare `proves: [AC-R<req>-<n>, ...]` — the structured
# acceptance-criterion ids that command proves. Optional: legacy tasks omit it and are
# unaffected. When present, entries SHOULD be AC-ids; a stray value is a non-blocking
# advisory here, and the enforce-gated *coverage* of `must` criteria lives in the
# traceability validator (BUILDER_TRACE_COVERAGE).
AC_ID_PATTERN = re.compile(r"^AC-R[0-9]+-[0-9]+$")


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="tasks",
        source_file="tasks.yaml",
        schema_file="tasks.schema.yaml",
        render=render_tasks,
        rendered_file="tasks.md",
        extra_validation=validate_tasks,
    )


def _verify_proves(verify_item: Any) -> list[str]:
    """The acceptance-criterion ids a single verify item claims to prove. Empty when the
    optional `proves` field is absent — that is not an error (legacy verify items)."""
    if isinstance(verify_item, dict):
        return string_list(verify_item.get("proves"))
    return []


def collect_proves_ids(data: dict[str, Any]) -> set[str]:
    """All acceptance-criterion ids referenced by any task's verify[].proves (plus any
    task-level `proves`). Returned as a set for the traceability acceptance-coverage
    check; an empty result means no task claims to prove any structured criterion."""
    collected: set[str] = set()
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        collected.update(string_list(task.get("proves")))
        verify_raw = task.get("verify")
        verify_items = verify_raw if isinstance(verify_raw, list) else []
        for verify_item in verify_items:
            collected.update(_verify_proves(verify_item))
    return collected


def _command_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("command", ""))
    return str(item)


def _segment_probative(segment: str) -> bool:
    """True if one simple shell command can prove something under the exit-code-only gate."""
    seg = segment.strip()
    if not seg:
        return False
    if seg.startswith("!"):
        # Evaluate the negated command: `! grep …` is probative (encodes success in
        # exit 0); `! true`/`! false` are constant tautologies and prove nothing.
        return _segment_probative(seg[1:].strip())
    tokens = seg.split()
    lead = tokens[0]
    if lead in ("true", "false", ":"):
        return False
    if lead == "exit" and (len(tokens) == 1 or tokens[1] == "0"):
        return False
    if lead == "echo":
        return False
    if lead in ("ls", "cat"):
        # A path ARGUMENT makes these existence/readability assertions (non-zero on a
        # missing/unreadable path); only a bare `ls`/`cat` proves nothing.
        return len(tokens) > 1
    if lead == "grep":
        # Absence markers count only OUTSIDE quoted pattern text — a marker inside the
        # searched-for string (e.g. grep -q 'must not be called') is a PRESENCE grep,
        # not a zero-hits assertion, and is probative.
        unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", seg).lower()
        if any(marker in unquoted for marker in _ABSENCE_MARKERS):
            return False
        return True
    return True


def _non_probative_reason(command: str) -> str | None:
    """Reason string if `command` is a non-probative verify command (P0-1b denylist),
    else None. Models the exit-code-only gate's shell semantics (no pipefail): a
    failure-swallowing `|| true` tail neutralizes everything; a `;` chain exits with its
    LAST command; a pipeline exits with its LAST stage; an `&&` chain fails if any part
    fails (probative if any part is a real assertion)."""
    stripped = command.strip()
    if not stripped:
        return "empty verify command"
    # Failure-swallowing tail: `... || true`, `|| :`, `|| exit 0`, `|| echo …` -> always exit 0.
    if re.search(r"\|\|\s*(true|:|exit\s+0\b|echo\b)", stripped):
        return f"non-probative verify command `{stripped}`: a `|| true`/`|| echo` tail swallows failure (always exits 0)"
    last_seq = stripped.split(";")[-1].strip()            # `;` chain -> last command's status
    last_stage = re.split(r"(?<!\|)\|(?!\|)", last_seq)[-1].strip()  # pipeline (no pipefail) -> last stage
    if any(_segment_probative(part) for part in last_stage.split("&&")):
        return None
    lead = last_stage.split()[0].lstrip("!") if last_stage.split() else ""
    if lead == "grep":
        return "non-probative verify command: a zero-hits `grep` must be negated (`! grep …`)"
    return f"non-probative verify command `{stripped}`: encode success in exit code 0"


def _hermetic_lint_enforced() -> bool:
    """BUILDER_VERIFY_HERMETIC: off|warn|enforce (default warn). Staged like the other gates."""
    return (os.environ.get("BUILDER_VERIFY_HERMETIC", "warn") or "warn").strip().lower() == "enforce"


# ripgrep's -g/--glob/--iglob and -e/--regexp flags take a PATTERN or GLOB, never a filesystem
# path: `-g '!**/*.test.*'` is an exclusion glob whose leading `/` is glob syntax, not a hardcoded
# location, and `-e 'foo/bar'` is a search pattern, not a directory. When neither -e nor --regexp
# is given, ripgrep also accepts the search pattern as a bare positional argument (`rg 'pat' dir/`),
# so that positional gets the same treatment as an explicit -e value.
_RG_COMMAND_WORDS = ("rg", "ripgrep")
_RG_PATTERN_FLAGS = ("-e", "--regexp")
_RG_GLOB_FLAGS = ("-g", "--glob", "--iglob")
_RG_VALUE_FLAGS = _RG_PATTERN_FLAGS + _RG_GLOB_FLAGS
_RG_LONG_EQ_FLAGS = ("--glob=", "--iglob=", "--regexp=")
_RG_SEGMENT_BREAK = ("&&", "||", "|", ";", "(", ")")
_SHELL_WORD_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")


def _mask_ripgrep_pattern_and_glob_values(text: str) -> str:
    """Blank out (by span, preserving length) the VALUE of every ripgrep glob/pattern
    argument in `text` -- explicit -g/--glob/--iglob/-e/--regexp values, and the bare
    positional PATTERN ripgrep accepts when -e/--regexp isn't used -- so the hermeticity
    scan never mistakes a search pattern or exclusion glob for a filesystem path.

    Scoped narrowly to `rg`/`ripgrep` invocations only. Every other command, and every
    PATH argument to rg itself (quoted or not), is left untouched and still scanned.
    """
    tokens = list(_SHELL_WORD_RE.finditer(text))
    if not tokens:
        return text
    n = len(tokens)
    to_mask = [False] * len(text)

    def mask(start: int, end: int) -> None:
        for k in range(start, end):
            to_mask[k] = True

    i = 0
    while i < n:
        word = tokens[i].group(0)
        is_segment_start = i == 0 or tokens[i - 1].group(0) in _RG_SEGMENT_BREAK
        if word in _RG_COMMAND_WORDS and is_segment_start:
            j = i + 1
            saw_pattern_flag = False
            pattern_span: tuple[int, int] | None = None
            while j < n:
                tok = tokens[j]
                t = tok.group(0)
                if t in _RG_SEGMENT_BREAK:
                    break
                if t in _RG_VALUE_FLAGS:
                    if t in _RG_PATTERN_FLAGS:
                        saw_pattern_flag = True
                    if j + 1 < n and tokens[j + 1].group(0) not in _RG_SEGMENT_BREAK:
                        mask(tokens[j + 1].start(), tokens[j + 1].end())
                        j += 2
                        continue
                    j += 1
                    continue
                if t.startswith(_RG_LONG_EQ_FLAGS):
                    if t.startswith("--regexp="):
                        saw_pattern_flag = True
                    eq_at = tok.start() + t.index("=") + 1
                    mask(eq_at, tok.end())
                    j += 1
                    continue
                if t.startswith("-"):
                    j += 1  # some other boolean/short flag -- no value to mask
                    continue
                if pattern_span is None:
                    pattern_span = (tok.start(), tok.end())
                j += 1
            if pattern_span is not None and not saw_pattern_flag:
                mask(*pattern_span)
        i += 1

    if not any(to_mask):
        return text
    return "".join("_" if masked else ch for ch, masked in zip(text, to_mask))


def _non_hermetic_reason(command: str) -> str | None:
    """Reason string if `command` cannot be RE-RUN later, else None.

    A verify command is not a receipt -- it is a check, and a check that only works once, on one
    machine, at one path, is not a check. The host runs it with `cwd = project_dir`, so it must be
    expressed RELATIVE to the project.

    This rule was derived from data, not taste. `isanna model verify` re-ran every check every spec
    in this repo ever wrote: 134 failed, and **all 134 hardcoded an absolute repo path** (66 also
    hardcoded a `cd`). They fail in a worktree, in CI, on a laptop, and for anyone who installs this
    -- which means the accumulated corpus of checks could not be used to answer "what still works?".
    That is the SSOT's fuel, and it was contaminated at the source.
    """
    stripped = command.strip()
    if not stripped:
        return None  # emptiness is the denylist's business, not ours
    # 1. Any absolute filesystem token. The host already runs you AT the project root.
    # `/dev/null` is a shell sink rather than a location the check depends on.
    # URL paths are resources, not host filesystem dependencies. Remove any URI with an
    # RFC-style scheme before looking for absolute filesystem tokens. Also mask ripgrep
    # glob/pattern argument VALUES (see `_mask_ripgrep_pattern_and_glob_values`) -- a `/`
    # inside a `-g` exclusion glob or a search pattern is not a hardcoded location.
    path_scan = re.sub(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s;&|<>]+", "", stripped)
    path_scan = _mask_ripgrep_pattern_and_glob_values(path_scan)
    m = re.search(r"(?<![\w])(?:~|\$HOME)(?:/[^\s;&|<>]+)?", path_scan)
    if not m:
        m = re.search(r"(?<![\w/.])/(?!dev/null(?:\s|$|[;&|]))[^\s;&|<>]+", path_scan)
    if m:
        return (f"non-hermetic verify command: hardcodes the absolute path `{m.group(0)}` -- "
                "the host runs verify commands with cwd=<project root>, so use a RELATIVE path. "
                "An absolute path only works on one machine, at one checkout, and cannot be re-run.")
    # 2. Directory-changing commands: the harness owns the working directory. Relative `-C`
    # arguments remain portable and are intentionally allowed.
    if re.search(r"(^|&&|;|\|)\s*cd\s+[^\s;&|]+", stripped):
        return ("non-hermetic verify command: uses `cd` -- the host sets cwd=<project root> for you. "
                "A command that relocates itself cannot be re-run from a different checkout.")
    directory_change = re.search(
        r"(?:^|&&|;|\|)\s*(?:pushd\s+|(?:env|make|git)\s+-C\s+)(/[^\s;&|]*|~[^\s;&|]*|\$HOME(?:/[^\s;&|]*)?)",
        stripped,
    )
    if directory_change:
        return ("non-hermetic verify command: changes to an absolute directory -- the host sets "
                "cwd=<project root> for you. Use a RELATIVE directory so the check can be re-run.")
    # 3. Ambient temp dirs that must pre-exist.
    m = re.search(r"mktemp[^\n]*-p\s+(\S+)", stripped)
    if m:
        return (f"non-hermetic verify command: `mktemp -p {m.group(1)}` requires that directory to "
                "already exist. Use a plain `mktemp -d`.")
    return None


def validate_tasks(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    verify_warnings: list[str] = []
    proves_warnings: list[str] = []
    lint_enforced = _verify_lint_enforced()

    def lint(message: str) -> None:
        # Verify-command denylist is staged: hard error under enforce, advisory under warn.
        (errors if lint_enforced else verify_warnings).append(message)

    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    ids = [str(task.get("id", "")).strip() for task in tasks if isinstance(task, dict)]
    known_ids = set(ids)

    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        location = f"{source_name}.tasks[{index}]"
        # Human-gated tasks legitimately use non-machine sentinels (echo 'OPERATOR-GATED …'),
        # so the probative-verify lint does not apply to them.
        human_gated = bool(task.get("human_gate"))
        verify_raw = task.get("verify")
        if isinstance(verify_raw, list):
            verify_items = verify_raw
        elif isinstance(verify_raw, str) and verify_raw.strip():
            verify_items = [verify_raw]  # tolerate a scalar verify command
        else:
            verify_items = []
        if not verify_items and not human_gated:
            lint(f"{location}: verify must include at least one command")
        # `proves` shape advisory (non-blocking, applies to gated tasks too). Absent
        # `proves` is silent — legacy tasks are unaffected; only a present-but-malformed
        # AC-id reference is surfaced as a WARN.
        for proves_ref in string_list(task.get("proves")):
            if not AC_ID_PATTERN.match(proves_ref):
                proves_warnings.append(
                    f"{location}.proves references `{proves_ref}` which is not an AC-R<req>-<n> id"
                )
        for verify_index, verify_item in enumerate(verify_items, start=1):
            for proves_ref in _verify_proves(verify_item):
                if not AC_ID_PATTERN.match(proves_ref):
                    proves_warnings.append(
                        f"{location}.verify[{verify_index}].proves references `{proves_ref}` which is not an AC-R<req>-<n> id"
                    )
        if not human_gated:
            for verify_index, verify_item in enumerate(verify_items, start=1):
                nonprobative = _non_probative_reason(_command_text(verify_item))
                if nonprobative is not None:
                    lint(f"{location}.verify[{verify_index}]: {nonprobative}")
                # A check that cannot be RE-RUN later is a receipt, not a check. Staged separately
                # (BUILDER_VERIFY_HERMETIC) because the existing corpus violates it wholesale --
                # all 134 failing checks in this repo hardcode an absolute path.
                nonhermetic = _non_hermetic_reason(_command_text(verify_item))
                if nonhermetic is not None:
                    (errors if _hermetic_lint_enforced() else verify_warnings).append(
                        f"{location}.verify[{verify_index}]: {nonhermetic}")

        tdd = task.get("tdd") if isinstance(task.get("tdd"), dict) else {}
        mode = str(tdd.get("mode", "")).strip()
        reason = str(tdd.get("reason", "")).strip()
        if mode == "exempt" and reason not in VALID_TDD_EXEMPT_REASONS:
            errors.append(f"{location}.tdd.reason: expected one of {sorted(VALID_TDD_EXEMPT_REASONS)}")
        if mode == "required":
            files_field = " ".join(string_list(task.get("files"))).lower()
            has_test_file = "test" in files_field or "_test" in files_field or "/tests/" in files_field
            if not has_test_file:
                errors.append(f"{location}: TDD required but no test file found in files")
            steps = task.get("steps") if isinstance(task.get("steps"), list) else []
            first_step = ""
            if steps:
                first = steps[0]
                if isinstance(first, dict):
                    first_step = str(first.get("text", "")).lower()
                elif isinstance(first, str):
                    first_step = first.lower()
            if not steps or not ("fail" in first_step or "red" in first_step or ("write" in first_step and "test" in first_step)):
                errors.append(f"{location}: TDD required but the first step does not look like a RED step")
            if len(verify_items) < 2:
                errors.append(f"{location}: TDD required verify must include a focused test command and a project verification command")

        for field_name in ("depends_on", "parallel_with"):
            for ref in string_list(task.get(field_name)):
                if ref not in known_ids:
                    errors.append(f"{location}.{field_name}: unknown task id `{ref}`")

    for message in verify_warnings:
        print(f"WARN  {message} (BUILDER_VERIFY_LINT=warn)", file=sys.stderr)
    for message in proves_warnings:
        print(f"WARN  {message}", file=sys.stderr)
    return errors
