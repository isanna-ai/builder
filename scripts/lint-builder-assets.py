#!/usr/bin/env python3
"""Builder asset hygiene linter.

Checks prompt frontmatter, portable references, manifest consistency,
and status-value source-of-truth discipline.

Usage:
  lint-builder-assets.py [--manifest PATH] [--check-frontmatter]
                           [--check-references] [--check-manifest]
                           [--check-status-source-of-truth]
                           <root>

Exit codes:
  0 = all requested checks passed with zero violations
  1 = one or more violations found
  2 = usage / I/O error
"""

from __future__ import annotations

import argparse
import os
import re
import sys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_prompt_files(root: str) -> list[str]:
    """Return absolute paths to *.prompt.md files to lint.

    If <root>/prompts/ exists, scan only that directory (production use).
    Otherwise scan <root> directly — used for test-fixture roots that do not
    have a prompts/ subdirectory.
    """
    prompts_dir = os.path.join(root, "prompts")
    if os.path.isdir(prompts_dir):
        base = prompts_dir
    else:
        base = root
    return sorted(
        os.path.join(base, f)
        for f in os.listdir(base)
        if f.endswith(".prompt.md") and os.path.isfile(os.path.join(base, f))
    )


def _parse_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body) or (None, content) if no valid frontmatter."""
    if not content.startswith("---"):
        return None, content
    # Closing --- must be on its own line after the opening one
    rest = content[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end == -1:
        return None, content
    fm_text = rest[:end]
    body = rest[end + 4:]  # skip '\n---'
    return fm_text, body


def _strip_fenced_blocks(text: str) -> str:
    """Remove triple-backtick fenced code blocks from text."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_frontmatter(root: str) -> list[str]:
    """Enforce frontmatter contract on .prompt.md and SKILL.md files."""
    errors: list[str] = []

    for fpath in _find_prompt_files(root):
        relpath = os.path.relpath(fpath, root)
        try:
            content = open(fpath, encoding="utf-8").read()
        except OSError as exc:
            errors.append(f"{relpath}: io: cannot read file ({exc})")
            continue

        if not content.startswith("---"):
            errors.append(f"{relpath}: frontmatter: missing opening ---")
            continue

        fm_text, _ = _parse_frontmatter(content)
        if fm_text is None:
            errors.append(f"{relpath}: frontmatter: missing closing ---")
            continue

        if "agent: agent" not in fm_text:
            errors.append(f"{relpath}: frontmatter: missing 'agent: agent'")

        desc_match = re.search(r'description:\s*"?([^"\n]+)"?', fm_text)
        if not desc_match or not desc_match.group(1).strip():
            errors.append(f"{relpath}: frontmatter: missing or empty 'description'")

    # Also check SKILL.md files under <root>/skills/
    skills_dir = os.path.join(root, "skills")
    if os.path.isdir(skills_dir):
        for dirpath, _dirs, filenames in os.walk(skills_dir):
            for fname in filenames:
                if fname == "SKILL.md":
                    fpath = os.path.join(dirpath, fname)
                    relpath = os.path.relpath(fpath, root)
                    try:
                        content = open(fpath, encoding="utf-8").read()
                    except OSError as exc:
                        errors.append(f"{relpath}: io: cannot read file ({exc})")
                        continue
                    fm_text, _ = _parse_frontmatter(content)
                    if fm_text is not None:
                        desc_match = re.search(r'description:\s*"?([^"\n]+)"?', fm_text)
                        if not desc_match or not desc_match.group(1).strip():
                            errors.append(
                                f"{relpath}: frontmatter: missing or empty 'description'"
                            )

    return errors


def check_references(root: str) -> list[str]:
    """Detect hardcoded installation-surface paths in prompt body text."""
    errors: list[str] = []

    for fpath in _find_prompt_files(root):
        relpath = os.path.relpath(fpath, root)
        try:
            content = open(fpath, encoding="utf-8").read()
        except OSError as exc:
            errors.append(f"{relpath}: io: cannot read file ({exc})")
            continue

        _, body = _parse_frontmatter(content)
        clean_body = _strip_fenced_blocks(body)

        for lineno, line in enumerate(clean_body.splitlines(), start=1):
            if ".github/skills/" in line:
                errors.append(
                    f"{relpath}: hardcoded-ref: '.github/skills/' on line {lineno} "
                    f"— use {{{{BUILDER_ROOT}}}}/skills/ instead"
                )
            if ".builder/builder-" in line:
                errors.append(
                    f"{relpath}: hardcoded-ref: '.builder/builder-' on line {lineno} "
                    f"— use {{{{BUILDER_ROOT}}}}/standards/ instead"
                )

    return errors


def check_manifest(root: str, manifest_path: str) -> list[str]:
    """Verify manifest prompt entries match on-disk isanna-*.prompt.md files."""
    errors: list[str] = []

    prompts_dir = os.path.join(root, "prompts")

    # Load manifest prompt entries
    manifest_prompts: set[str] = set()
    try:
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("prompt "):
                    manifest_prompts.add(line[7:].strip())
    except OSError as exc:
        errors.append(f"manifest: io: cannot read {manifest_path} ({exc})")
        return errors

    # On-disk isanna-*.prompt.md files
    disk_prompts: set[str] = set()
    if os.path.isdir(prompts_dir):
        for fname in os.listdir(prompts_dir):
            if re.match(r"isanna-.+\.prompt\.md$", fname):
                disk_prompts.add(fname)

    for fname in sorted(manifest_prompts):
        if not os.path.isfile(os.path.join(prompts_dir, fname)):
            errors.append(
                f"prompts/{fname}: manifest: listed in manifest but missing from disk"
            )

    for fname in sorted(disk_prompts):
        if fname not in manifest_prompts:
            errors.append(
                f"prompts/{fname}: manifest: on-disk file not listed in manifest"
            )

    return errors


def check_status_source_of_truth(root: str) -> list[str]:
    """Flag status literal values in scripts/prompts that are not in the contract enum."""
    errors: list[str] = []

    contract_path = os.path.join(root, "standards", "builder-contract.md")
    known_statuses: set[str] = set()

    if os.path.isfile(contract_path):
        try:
            content = open(contract_path, encoding="utf-8").read()
        except OSError:
            content = ""
        m = re.search(r"```yaml status-enum\n(.*?)```", content, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                val = line.strip().lstrip("- ").strip("\"'")
                if val:
                    known_statuses.add(val)

    if not known_statuses:
        errors.append(
            f"standards/builder-contract.md: status-source-of-truth: no "
            f"```yaml status-enum block found under '## Machine-readable "
            f"Appendix' (cannot verify status literals against the contract)"
        )
        return errors

    for subdir in ("scripts", "prompts"):
        scan_dir = os.path.join(root, subdir)
        if not os.path.isdir(scan_dir):
            continue
        for fname in sorted(os.listdir(scan_dir)):
            if not (fname.endswith(".py") or fname.endswith(".md")):
                continue
            fpath = os.path.join(scan_dir, fname)
            if not os.path.isfile(fpath):
                continue
            relpath = os.path.relpath(fpath, root)
            try:
                lines = open(fpath, encoding="utf-8").readlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, start=1):
                m2 = re.search(r"\bstatus:\s+([a-z_-]+)", line)
                if m2:
                    val = m2.group(1)
                    # Skip matches inside backtick-quoted documentation text
                    # (e.g. `status: done` in a comment or prompt description).
                    if f"`status: {val}`" in line or f"`status:{val}`" in line:
                        continue
                    # Skip Python type annotations (`status: str`, `status: str
                    # | None`, dataclass/argument annotations) — not a status
                    # literal, "str" is never a valid status value.
                    if val == "str":
                        continue
                    if val not in known_statuses:
                        errors.append(
                            f"{relpath}: status-source-of-truth: unknown status "
                            f"value '{val}' on line {lineno}"
                        )

    return errors


_CAPABILITY_CLASSES = {
    "deep_reasoner", "independent_reviewer", "structured_planner",
    "fast_editor", "broad_context_explorer",
}


def _norm_model(cell: str) -> str:
    """Normalize a model id for doc<->registry comparison.

    Strips backticks/space, drops a leading `claude-` vendor prefix, and unifies
    `.`/`-` so the doc's readable short ids (`opus-4.8`) compare equal to the
    registry's full ids (`claude-opus-4-8`).
    """
    s = (cell or "").strip().strip("`").strip().lower()
    if s.startswith("claude-"):
        s = s[len("claude-"):]
    return s.replace(".", "-")


def _load_registry(root: str):
    """Import CAPABILITY_MODEL_MAP / CAPABILITY_EFFORT_MAP from the target root."""
    scripts_dir = os.path.join(root, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from _dispatch_runtime import model_registry
    return model_registry.CAPABILITY_MODEL_MAP, model_registry.CAPABILITY_EFFORT_MAP


def _parse_capability_table(content: str) -> dict[str, dict[str, str]]:
    """Parse the §4 capability->model table into
    {class: {codex_model, codex_effort, claude_model, claude_effort}}.

    Recognizes the table by rows whose first cell is a known capability class,
    so it is robust to the table's line position and surrounding prose.
    """
    rows: dict[str, dict[str, str]] = {}
    for line in content.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        cap = cells[0]
        if cap not in _CAPABILITY_CLASSES:
            continue
        # columns: class | purpose | codex model | codex effort | claude model | claude effort
        rows[cap] = {
            "codex_model": cells[2],
            "codex_effort": cells[3].lower(),
            "claude_model": cells[4],
            "claude_effort": cells[5].lower(),
        }
    return rows


def check_model_registry_drift(root: str) -> list[str]:
    """Flag drift between the §4 capability->model table (builder-workflow.md)
    and model_registry.py (the runtime source of truth)."""
    errors: list[str] = []
    doc_path = os.path.join(root, "standards", "builder-workflow.md")
    if not os.path.isfile(doc_path):
        return errors  # doc not present in this root; skip silently
    try:
        content = open(doc_path, encoding="utf-8").read()
    except OSError:
        return errors
    try:
        model_map, effort_map = _load_registry(root)
    except Exception as exc:  # registry not importable from this root
        return [f"model-registry-drift: could not import model_registry: {exc}"]

    doc_rows = _parse_capability_table(content)
    if not doc_rows:
        return ["model-registry-drift: could not find the §4 capability->model "
                "table in standards/builder-workflow.md"]

    reg_classes = set(model_map.keys())
    doc_classes = set(doc_rows.keys())
    for missing in sorted(reg_classes - doc_classes):
        errors.append(f"model-registry-drift: registry class '{missing}' missing from the §4 doc table")
    for extra in sorted(doc_classes - reg_classes):
        errors.append(f"model-registry-drift: §4 doc table has unknown class '{extra}' (not in registry)")

    for cap in sorted(reg_classes & doc_classes):
        doc = doc_rows[cap]
        model_checks = (
            ("codex model", doc["codex_model"], model_map[cap].get("codex-cli", "")),
            ("claude model", doc["claude_model"], model_map[cap].get("claude-code-cli", "")),
        )
        for label, doc_val, reg_val in model_checks:
            if _norm_model(doc_val) != _norm_model(reg_val):
                errors.append(f"model-registry-drift: {cap} {label}: doc='{doc_val}' registry='{reg_val}'")
        effort_checks = (
            ("codex effort", doc["codex_effort"], effort_map.get(cap, {}).get("codex-cli", "")),
            ("claude effort", doc["claude_effort"], effort_map.get(cap, {}).get("claude-code-cli", "")),
        )
        for label, doc_val, reg_val in effort_checks:
            if doc_val.strip().lower() != reg_val.strip().lower():
                errors.append(f"model-registry-drift: {cap} {label}: doc='{doc_val}' registry='{reg_val}'")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Builder asset hygiene linter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", help="Root directory to lint (builder/ canonical or install root)")
    parser.add_argument("--manifest", help="Path to asset-manifest.txt (required for --check-manifest)")
    parser.add_argument("--check-frontmatter", action="store_true",
                        help="Check .prompt.md frontmatter (opening ---, agent: agent, description)")
    parser.add_argument("--check-references", action="store_true",
                        help="Check prompt bodies for hardcoded .github/skills/ and .builder/builder- refs")
    parser.add_argument("--check-manifest", action="store_true",
                        help="Check that manifest prompt entries match on-disk isanna-*.prompt.md files")
    parser.add_argument("--check-status-source-of-truth", action="store_true",
                        help="Check that status values in scripts/prompts are all in the contract enum")
    parser.add_argument("--check-model-registry-drift", action="store_true",
                        help="Check that the workflow §4 capability->model table matches model_registry.py")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: root directory does not exist: {root}", file=sys.stderr)
        return 2

    all_errors: list[str] = []

    if args.check_frontmatter:
        all_errors.extend(check_frontmatter(root))

    if args.check_references:
        all_errors.extend(check_references(root))

    if args.check_manifest:
        if not args.manifest:
            print("error: --check-manifest requires --manifest <path>", file=sys.stderr)
            return 2
        all_errors.extend(check_manifest(root, args.manifest))

    if args.check_status_source_of_truth:
        all_errors.extend(check_status_source_of_truth(root))

    if args.check_model_registry_drift:
        all_errors.extend(check_model_registry_drift(root))

    for err in all_errors:
        print(err)

    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
