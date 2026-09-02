#!/usr/bin/env python3
"""Backfill traceability D-ids across existing Builder specs.

Prepares specs so `BUILDER_TRACE_COVERAGE` can be flipped to `enforce` and the
design `id` field made required (`^D[0-9]+$`). Existing `design.yaml` files carry
FREE-FORM ids (e.g. `billing-role-migration`, `audit_coverage_invariant`) or none;
`traceability.yaml` design links reference those free-form ids (or titles). This
tool normalizes design ids to `D<n>`, rewrites the traceability references so the
links stay consistent, and prints a GAP REPORT of the trace-link holes a human must
fill. It NEVER invents links.

DRY-RUN IS THE DEFAULT. Only `--apply` writes files. It writes ONLY `design.yaml`
(id fields) and `traceability.yaml` (design reference fields). It never touches
requirement statements, tasks, evidence, rendered `.md` views, runtime/queue state,
or anything else. It is idempotent: re-running an already-migrated spec is a no-op.

Usage:
    python3 scripts/backfill-traceability.py <spec-dir> [--apply]
    python3 scripts/backfill-traceability.py --all <root> [--apply]

`<spec-dir>` is a path to a single spec directory (containing design.yaml).
`--all <root>` sweeps every `<root>/.builder/specs/*` directory.

Design decisions (why line-based surgery, not a YAML round-trip):
  The repo ships a deliberately lossy `_yaml_compat.py` fallback (no ruamel / no PyYAML in the
  standard runtime). Dumping parsed data back out would flatten block scalars (`>-`),
  drop comments, and reflow lists — a destructive, non-idempotent rewrite. Instead
  this tool reads structure with the repo's own parser for the GAP REPORT, but for
  WRITING it performs targeted text edits on exactly the `id:` lines (design.yaml)
  and the `design_id:` / `design_ids:` reference tokens (traceability.yaml), leaving
  every other byte untouched. That keeps diffs minimal and re-runs a true no-op.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Any, Optional

# Make the sibling `_validators` package importable regardless of how we are
# launched (mirrors how validate-spec.py relies on the script dir being on path).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:  # Reuse the validator's readers so the gap report matches validate-spec.py.
    from _validators.common import parse_yaml_like_file, string_list  # type: ignore
except Exception:  # pragma: no cover - fallback keeps the tool standalone.
    parse_yaml_like_file = None  # type: ignore
    string_list = None  # type: ignore


DID_RE = re.compile(r"^D[0-9]+$")
# Canonical D-id assignment order. The task fixes this ordering explicitly:
# core_changes first, then responsibility_allocation, each in document order.
DESIGN_SECTIONS = ("core_changes", "responsibility_allocation")
# The "title" used as a secondary rewrite key differs per section.
SECTION_LABEL_FIELD = {"core_changes": "title", "responsibility_allocation": "surface"}


# ----------------------------------------------------------------------------- #
# Small helpers                                                                  #
# ----------------------------------------------------------------------------- #

def _split_lines(text: str) -> list[str]:
    """Split preserving the trailing-newline structure (rejoin with '\n')."""
    return text.split("\n")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_scalar(raw: str) -> str:
    """Trim, drop an inline `# comment`, and remove one layer of surrounding quotes."""
    value = raw.strip()
    # Strip a trailing comment only when it is clearly a comment (space before #).
    hash_idx = value.find(" #")
    if hash_idx != -1 and not (value[:1] in "'\""):
        value = value[:hash_idx].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.strip()


def _parse_yaml_text(text: str) -> dict[str, Any]:
    """Parse YAML text to a mapping using the same machinery as the validator.

    Used only for the READ-ONLY gap report (never for writing). Falls back to the
    validator's internal parser if real PyYAML is unavailable, matching runtime.
    """
    try:
        from _yaml import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            from _validators.common import _parse_entry  # type: ignore

            data = _parse_entry(text.splitlines())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _string_list(value: Any) -> list[str]:
    if string_list is not None:
        return string_list(value)  # type: ignore
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s or s == "[]":
            return []
        if s.startswith("[") and s.endswith("]"):
            return [p.strip().strip("'\"") for p in s[1:-1].split(",") if p.strip()]
        return [s]
    return [str(value).strip()]


# ----------------------------------------------------------------------------- #
# design.yaml scanning + D-id assignment                                        #
# ----------------------------------------------------------------------------- #

@dataclass
class DesignItem:
    section: str
    item_start: int            # line index of the `- ` header line
    item_end: int              # exclusive end line index of the item
    item_indent: int
    field_indent: int
    id_value: Optional[str]    # existing id (unquoted) or None
    id_line_idx: Optional[int]
    label: Optional[str]       # title (core_changes) or surface (resp_alloc), single-line only
    first_field_end: int       # last line index belonging to the FIRST field (for insertion)
    final_id: str = ""         # assigned after the fact


def _top_level_block_body(lines: list[str], key: str) -> Optional[tuple[int, int, int]]:
    """Return (header_idx, body_start, body_end) for a top-level `key:` block.

    Only matches a block form (`key:` with an empty value). An inline form such as
    `core_changes: []` returns None (no items to number).
    """
    header_idx = None
    for i, line in enumerate(lines):
        if _indent_of(line) != 0:
            continue
        m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if not m:
            continue
        if m.group(1) == key:
            if m.group(2).strip():  # inline value (e.g. `[]`) -> no block items
                return None
            header_idx = i
            break
    if header_idx is None:
        return None
    body_start = header_idx + 1
    body_end = len(lines)
    for j in range(body_start, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        if _indent_of(line) == 0:
            body_end = j
            break
    return header_idx, body_start, body_end


def _scan_section(lines: list[str], section: str) -> list[DesignItem]:
    block = _top_level_block_body(lines, section)
    if block is None:
        return []
    _, body_start, body_end = block

    # Item indent = the indent of the first `- ` list marker in the block body.
    item_indent = None
    for j in range(body_start, body_end):
        line = lines[j]
        if not line.strip():
            continue
        if re.match(r"^\s*- ", line):
            item_indent = _indent_of(line)
            break
    if item_indent is None:
        return []

    # Item start line indices.
    starts = [
        j
        for j in range(body_start, body_end)
        if _indent_of(lines[j]) == item_indent and re.match(r"^\s*- ", lines[j])
    ]
    items: list[DesignItem] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else body_end
        items.append(_scan_item(lines, section, start, end, item_indent))
    return items


def _scan_item(lines: list[str], section: str, start: int, end: int, item_indent: int) -> DesignItem:
    field_indent = item_indent + 2
    head = lines[start][item_indent + 2 :]  # text after "- "
    head_key = None
    head_val = None
    hm = re.match(r"^(\w[\w-]*):\s*(.*)$", head)
    if hm:
        head_key, head_val = hm.group(1), hm.group(2)

    # Determine the last line belonging to the FIRST field (insertion anchor).
    first_field_end = start
    head_is_block = head_val is None or head_val.strip() == "" or head_val.strip()[:1] in (">", "|")
    if head_is_block:
        for j in range(start + 1, end):
            if not lines[j].strip():
                continue
            if _indent_of(lines[j]) > field_indent:
                first_field_end = j
            else:
                break

    # Collect shallow single-line fields (id / label) from the item.
    id_value: Optional[str] = None
    id_line_idx: Optional[int] = None
    label: Optional[str] = None
    label_field = SECTION_LABEL_FIELD[section]

    if head_key == "id" and head_val is not None:
        id_value = _strip_scalar(head_val)
        id_line_idx = start
    if head_key == label_field and head_val is not None and head_val.strip() and not head_is_block:
        label = _strip_scalar(head_val)

    for j in range(start + 1, end):
        line = lines[j]
        if not line.strip() or _indent_of(line) != field_indent:
            continue
        fm = re.match(r"^\s*(\w[\w-]*):\s*(.*)$", line)
        if not fm:
            continue
        fkey, fval = fm.group(1), fm.group(2)
        if fkey == "id" and id_value is None:
            id_value = _strip_scalar(fval)
            id_line_idx = j
        elif fkey == label_field and label is None and fval.strip() and fval.strip()[:1] not in (">", "|"):
            label = _strip_scalar(fval)

    return DesignItem(
        section=section,
        item_start=start,
        item_end=end,
        item_indent=item_indent,
        field_indent=field_indent,
        id_value=id_value,
        id_line_idx=id_line_idx,
        label=label,
        first_field_end=first_field_end,
    )


def _assign_dids(items: list[DesignItem]) -> None:
    """Assign final D-ids in canonical order, preserving any existing `^D[0-9]+$`.

    Idempotency: an entry that already carries a D-id KEEPS it (no churn). Fresh ids
    fill the lowest unused integers, so a fully-fresh spec numbers cleanly D1,D2,...
    and a fully-migrated spec (D1..Dn already in order) is a no-op.
    """
    used: set[int] = set()
    for it in items:
        if it.id_value and DID_RE.match(it.id_value):
            used.add(int(it.id_value[1:]))
    next_n = 1
    for it in items:
        if it.id_value and DID_RE.match(it.id_value):
            it.final_id = it.id_value
            continue
        while next_n in used:
            next_n += 1
        it.final_id = f"D{next_n}"
        used.add(next_n)
        next_n += 1


def _rewrite_id_line(line: str, new_id: str) -> str:
    """Replace the id value on an existing `id:` line, preserving prefix + comment."""
    m = re.match(r"^(?P<prefix>\s*(?:-\s+)?id:\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]+)(?P<rest>.*)$", line)
    if not m:
        return line
    return f"{m.group('prefix')}{new_id}{m.group('rest')}"


@dataclass
class DesignPlan:
    items: list[DesignItem]
    new_text: Optional[str]     # None when design.yaml unreadable/absent
    changed: bool
    # (final_id, old_id_or_None, section, status) for the printed mapping.
    mapping: list[tuple[str, Optional[str], str, str]]
    rename_map: dict[str, str]  # old free-form id / unique label -> final D-id
    final_ids: set[str]
    error: Optional[str] = None


def plan_design(text: Optional[str]) -> DesignPlan:
    if text is None:
        return DesignPlan([], None, False, [], {}, set(), error="design.yaml not found")

    lines = _split_lines(text)
    items: list[DesignItem] = []
    for section in DESIGN_SECTIONS:
        items.extend(_scan_section(lines, section))

    _assign_dids(items)

    # Build the rewrite map for traceability: old free-form ids win, then unique labels.
    rename_map: dict[str, str] = {}
    final_ids = {it.final_id for it in items}
    for it in items:
        if it.id_value and not DID_RE.match(it.id_value):
            rename_map[it.id_value] = it.final_id
    label_counts = Counter(it.label for it in items if it.label)
    for it in items:
        if it.label and label_counts[it.label] == 1 and it.label not in rename_map and it.label not in final_ids:
            rename_map[it.label] = it.final_id

    # Compute the surgical design edits (apply bottom-up to keep indices stable).
    edits: list[tuple[int, str]] = []      # (line_idx, replacement) for replace
    inserts: list[tuple[int, str]] = []    # (after_line_idx, new_line) for insert
    mapping: list[tuple[str, Optional[str], str, str]] = []
    for it in items:
        if it.id_value and DID_RE.match(it.id_value):
            status = "kept"
            mapping.append((it.final_id, it.id_value, it.section, status))
            continue
        if it.id_line_idx is not None:  # has a free-form id -> rewrite in place
            new_line = _rewrite_id_line(lines[it.id_line_idx], it.final_id)
            if new_line != lines[it.id_line_idx]:
                edits.append((it.id_line_idx, new_line))
            mapping.append((it.final_id, it.id_value, it.section, "renamed"))
        else:  # no id -> insert a new id line as the first field
            new_line = " " * it.field_indent + f"id: {it.final_id}"
            inserts.append((it.first_field_end, new_line))
            mapping.append((it.final_id, None, it.section, "added"))

    new_lines = list(lines)
    for line_idx, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        new_lines[line_idx] = replacement
    for after_idx, new_line in sorted(inserts, key=lambda e: e[0], reverse=True):
        new_lines.insert(after_idx + 1, new_line)

    new_text = "\n".join(new_lines)
    changed = new_text != text
    return DesignPlan(items, new_text, changed, mapping, rename_map, final_ids)


# ----------------------------------------------------------------------------- #
# traceability.yaml reference rewriting                                          #
# ----------------------------------------------------------------------------- #

_BLOCK_START_RE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?design_ids:\s*$")
_INLINE_LIST_RE = re.compile(r"^(?P<prefix>\s*(?:-\s+)?design_ids:\s*)\[(?P<inner>[^\]]*)\](?P<rest>.*)$")
_SCALAR_RE = re.compile(r"^(?P<prefix>\s*(?:-\s+)?design_id:\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]+)(?P<rest>.*)$")
_BLOCK_ITEM_RE = re.compile(r"^(?P<prefix>\s*-\s+)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]+)(?P<rest>.*)$")


@dataclass
class TracePlan:
    new_text: Optional[str]
    changed: bool
    rewrites: list[tuple[str, str, str]]     # (context, old, new)
    unresolved: list[tuple[str, str]]        # (context, token)
    present: bool = True


def _rewrite_token(
    raw_token: str,
    rename_map: dict[str, str],
    final_ids: set[str],
    context: str,
    rewrites: list,
    unresolved: list,
) -> str:
    """Return the replacement scalar text for one reference token."""
    token = _strip_scalar(raw_token)
    if token in rename_map:
        new = rename_map[token]
        if new != token:
            rewrites.append((context, token, new))
        return new
    if token in final_ids:
        return token  # already a valid final D-id -> leave untouched
    unresolved.append((context, token))
    return token  # leave untouched; a human must fix it


def plan_traceability(
    text: Optional[str], rename_map: dict[str, str], final_ids: set[str]
) -> TracePlan:
    if text is None:
        return TracePlan(None, False, [], [], present=False)

    lines = _split_lines(text)
    out: list[str] = []
    rewrites: list[tuple[str, str, str]] = []
    unresolved: list[tuple[str, str]] = []
    in_block = False
    block_base = 0
    i = 0
    while i < len(lines):
        line = lines[i]

        if in_block:
            m = _BLOCK_ITEM_RE.match(line)
            if m and _indent_of(line) > block_base:
                new_val = _rewrite_token(
                    m.group("val"), rename_map, final_ids, "design_ids[]", rewrites, unresolved
                )
                out.append(f"{m.group('prefix')}{new_val}{m.group('rest')}")
                i += 1
                continue
            in_block = False  # fall through to normal handling of this line

        mblock = _BLOCK_START_RE.match(line)
        if mblock:
            in_block = True
            block_base = len(mblock.group("indent"))
            out.append(line)
            i += 1
            continue

        minline = _INLINE_LIST_RE.match(line)
        if minline:
            inner = minline.group("inner")
            if inner.strip():
                new_tokens = []
                for part in inner.split(","):
                    new_tokens.append(
                        _rewrite_token(part, rename_map, final_ids, "design_ids", rewrites, unresolved)
                    )
                new_inner = ", ".join(new_tokens)
            else:
                new_inner = inner
            out.append(f"{minline.group('prefix')}[{new_inner}]{minline.group('rest')}")
            i += 1
            continue

        mscalar = _SCALAR_RE.match(line)
        if mscalar:
            new_val = _rewrite_token(
                mscalar.group("val"), rename_map, final_ids, "design_id", rewrites, unresolved
            )
            out.append(f"{mscalar.group('prefix')}{new_val}{mscalar.group('rest')}")
            i += 1
            continue

        out.append(line)
        i += 1

    new_text = "\n".join(out)
    return TracePlan(new_text, new_text != text, rewrites, unresolved)


# ----------------------------------------------------------------------------- #
# Gap report (read-only)                                                         #
# ----------------------------------------------------------------------------- #

def _read_map(path: Path) -> dict[str, Any]:
    if parse_yaml_like_file is not None:
        data, errors = parse_yaml_like_file(path)  # type: ignore
        return {} if errors else data
    try:
        return _parse_yaml_text(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _collect_requirement_ids(spec_dir: Path) -> list[str]:
    data = _read_map(spec_dir / "requirements.yaml")
    reqs = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    return [str(r.get("id", "")).strip() for r in reqs if isinstance(r, dict) and str(r.get("id", "")).strip()]


def _collect_must_acceptance_ids(spec_dir: Path) -> list[str]:
    data = _read_map(spec_dir / "requirements.yaml")
    reqs = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    out: list[str] = []
    for r in reqs:
        if not isinstance(r, dict):
            continue
        for a in r.get("acceptance") if isinstance(r.get("acceptance"), list) else []:
            if isinstance(a, dict) and str(a.get("priority", "")).strip() == "must":
                aid = str(a.get("id", "")).strip()
                if aid:
                    out.append(aid)
    return out


def _collect_task_ids(spec_dir: Path) -> list[str]:
    data = _read_map(spec_dir / "tasks.yaml")
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    return [str(t.get("id", "")).strip() for t in tasks if isinstance(t, dict) and str(t.get("id", "")).strip()]


def _collect_proves_refs(spec_dir: Path) -> set[str]:
    data = _read_map(spec_dir / "tasks.yaml")
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    out: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            continue
        out.update(_string_list(t.get("proves")))
        for v in t.get("verify") if isinstance(t.get("verify"), list) else []:
            if isinstance(v, dict):
                out.update(_string_list(v.get("proves")))
    return out


def gap_report(
    spec_dir: Path,
    design_final_ids: list[str],
    trace_text: Optional[str],
    trace_unresolved: list[tuple[str, str]],
) -> list[str]:
    gaps: list[str] = []

    if trace_text is None:
        n_req = len(_collect_requirement_ids(spec_dir))
        n_des = len(design_final_ids)
        n_task = len(_collect_task_ids(spec_dir))
        gaps.append(
            "traceability.yaml is missing — no requirement/design/task links exist yet "
            f"({n_req} requirement(s), {n_des} design(s), {n_task} task(s) to link)"
        )
        for ctx, tok in trace_unresolved:
            gaps.append(f"unresolved design reference in {ctx}: `{tok}` matches no design id/title")
        return gaps

    trace = _parse_yaml_text(trace_text)

    requirement_ids = _collect_requirement_ids(spec_dir)
    req_links = trace.get("requirement_links") if isinstance(trace.get("requirement_links"), list) else []
    linked_reqs: set[str] = set()
    for idx, e in enumerate(req_links, start=1):
        if not isinstance(e, dict):
            continue
        rid = str(e.get("requirement_id", "")).strip()
        if rid:
            linked_reqs.add(rid)
        if not _string_list(e.get("design_ids")):
            gaps.append(f"requirement_links[{idx}] (`{rid}`) has empty design_ids — not covered by any design")
    for rid in requirement_ids:
        if rid not in linked_reqs:
            gaps.append(f"requirement `{rid}` has no requirement_links entry — not traced to any design")

    des_links = trace.get("design_links") if isinstance(trace.get("design_links"), list) else []
    linked_designs: set[str] = set()
    for idx, e in enumerate(des_links, start=1):
        if not isinstance(e, dict):
            continue
        did = str(e.get("design_id", "")).strip()
        if did:
            linked_designs.add(did)
        if not _string_list(e.get("task_ids")):
            gaps.append(f"design_links[{idx}] (`{did}`) has empty task_ids — design not decomposed into any task")
    for did in design_final_ids:
        if did not in linked_designs:
            gaps.append(f"design `{did}` has no design_links entry — not decomposed into any task")

    task_ids = _collect_task_ids(spec_dir)
    task_links = trace.get("task_links") if isinstance(trace.get("task_links"), list) else []
    evidenced_tasks: set[str] = set()
    for idx, e in enumerate(task_links, start=1):
        if not isinstance(e, dict):
            continue
        tid = str(e.get("task_id", "")).strip()
        if _string_list(e.get("evidence_ids")):
            evidenced_tasks.add(tid)
        else:
            gaps.append(f"task_links[{idx}] (`{tid}`) has empty evidence_ids — task has no evidence")
    for tid in task_ids:
        if tid not in evidenced_tasks:
            gaps.append(f"task `{tid}` has no evidence (no task_links entry or empty evidence_ids)")

    proves = _collect_proves_refs(spec_dir)
    for aid in _collect_must_acceptance_ids(spec_dir):
        if aid not in proves:
            gaps.append(f"acceptance criterion `{aid}` (priority: must) is not proven by any task verify[].proves")

    for ctx, tok in trace_unresolved:
        gaps.append(f"unresolved design reference in {ctx}: `{tok}` matches no design id/title")

    return gaps


# ----------------------------------------------------------------------------- #
# Per-spec driver + reporting                                                    #
# ----------------------------------------------------------------------------- #

@dataclass
class SpecResult:
    spec_dir: Path
    design: DesignPlan
    trace: TracePlan
    gaps: list[str]
    applied: bool = False
    wrote: list[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def process_spec(spec_dir: Path, apply: bool) -> SpecResult:
    design_path = spec_dir / "design.yaml"
    trace_path = spec_dir / "traceability.yaml"

    design_text = _read_text(design_path) if design_path.exists() else None
    if design_text is None:
        return SpecResult(
            spec_dir,
            DesignPlan([], None, False, [], {}, set(), error="design.yaml not found"),
            TracePlan(None, False, [], [], present=False),
            [],
            skipped_reason="no design.yaml",
        )

    design = plan_design(design_text)
    trace_text = _read_text(trace_path) if trace_path.exists() else None
    trace = plan_traceability(trace_text, design.rename_map, design.final_ids)

    design_final_ids = [it.final_id for it in design.items]
    # The gap report reflects the POST-rewrite traceability content.
    gaps = gap_report(spec_dir, design_final_ids, trace.new_text, trace.unresolved)

    result = SpecResult(spec_dir, design, trace, gaps)

    if apply:
        if design.changed and design.new_text is not None:
            design_path.write_text(design.new_text, encoding="utf-8")
            result.wrote.append("design.yaml")
        if trace.present and trace.changed and trace.new_text is not None:
            trace_path.write_text(trace.new_text, encoding="utf-8")
            result.wrote.append("traceability.yaml")
        result.applied = True

    return result


def print_result(result: SpecResult, apply: bool) -> None:
    name = result.spec_dir.name
    print(f"\n=== spec: {name} ({result.spec_dir}) ===")

    if result.skipped_reason:
        print(f"  SKIPPED: {result.skipped_reason}")
        return

    # D-id mapping.
    print("  [design.yaml] D-id assignment:")
    if not result.design.mapping:
        print("    (no core_changes / responsibility_allocation entries found)")
    for final_id, old_id, section, status in result.design.mapping:
        origin = f"was `{old_id}`" if old_id is not None else "no prior id"
        print(f"    {final_id:<5} {section:<26} {status:<8} ({origin})")
    if result.design.changed:
        n_ren = sum(1 for _, o, _, s in result.design.mapping if s == "renamed")
        n_add = sum(1 for _, o, _, s in result.design.mapping if s == "added")
        print(f"  [design.yaml] changes: {n_ren} id(s) renamed, {n_add} id(s) inserted")
    else:
        print("  [design.yaml] changes: none (already normalized)")

    # Traceability rewrites.
    if not result.trace.present:
        print("  [traceability.yaml] absent — nothing to rewrite")
    elif result.trace.rewrites:
        print("  [traceability.yaml] reference rewrites:")
        for ctx, old, new in result.trace.rewrites:
            print(f"    {ctx}: {old} -> {new}")
    else:
        print("  [traceability.yaml] reference rewrites: none")
    if result.trace.unresolved:
        print("  [traceability.yaml] UNRESOLVED references (left untouched):")
        for ctx, tok in result.trace.unresolved:
            print(f"    {ctx}: `{tok}` matches no design id/title")

    # Gap report.
    if result.gaps:
        print("  [gap report] trace-link holes a human must fill:")
        for g in result.gaps:
            print(f"    - {g}")
    else:
        print("  [gap report] no gaps detected")

    # Mode.
    if apply:
        if result.wrote:
            print(f"  APPLIED: wrote {', '.join(result.wrote)}")
        else:
            print("  APPLIED: no changes needed (idempotent no-op)")
    else:
        would = []
        if result.design.changed:
            would.append("design.yaml")
        if result.trace.present and result.trace.changed:
            would.append("traceability.yaml")
        target = ", ".join(would) if would else "nothing (already normalized)"
        print(f"  DRY-RUN: no files written; --apply would write: {target}")


def _iter_all_specs(root: Path) -> list[Path]:
    specs_dir = runtime_dir(root) / "specs"
    if not specs_dir.is_dir():
        return []
    return sorted(p for p in specs_dir.iterdir() if p.is_dir())


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill traceability D-ids and report trace-link gaps (dry-run by default).",
    )
    parser.add_argument("spec_dir", nargs="?", help="Path to a single spec directory (with design.yaml)")
    parser.add_argument("--all", metavar="ROOT", help="Sweep every spec under ROOT's active runtime directory")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run, writes nothing)")
    args = parser.parse_args(argv)

    if bool(args.spec_dir) == bool(args.all):
        parser.error("provide exactly one of <spec-dir> or --all <root>")

    if args.all:
        root = Path(args.all).resolve()
        specs = _iter_all_specs(root)
        if not specs:
            print(f"No specs found under {runtime_dir(root) / 'specs'}", file=sys.stderr)
            return 1
        results: list[SpecResult] = []
        for spec in specs:
            result = process_spec(spec, args.apply)
            results.append(result)
            print_result(result, args.apply)

        # Aggregate summary.
        processed = [r for r in results if not r.skipped_reason]
        n_changed = sum(1 for r in processed if r.design.changed or (r.trace.present and r.trace.changed))
        n_gaps = sum(1 for r in processed if r.gaps)
        n_unres = sum(len(r.trace.unresolved) for r in processed)
        n_skipped = sum(1 for r in results if r.skipped_reason)
        print("\n=== summary ===")
        print(f"  specs scanned:       {len(results)}")
        print(f"  specs with edits:    {n_changed}")
        print(f"  specs with gaps:     {n_gaps}")
        print(f"  unresolved refs:     {n_unres}")
        print(f"  skipped (no design): {n_skipped}")
        if not args.apply:
            print("  MODE: DRY-RUN (no files written; re-run with --apply to write)")
        else:
            print("  MODE: APPLIED")
        return 0

    spec_dir = Path(args.spec_dir).resolve()
    if not spec_dir.is_dir():
        print(f"Not a directory: {spec_dir}", file=sys.stderr)
        return 1
    result = process_spec(spec_dir, args.apply)
    print_result(result, args.apply)
    if result.skipped_reason:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
