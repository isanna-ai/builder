"""P0.1 — the self-contained runner-packet contract (EMITTER + flag-gated VALIDATOR).

The runner packet (``runs/task-<id>.yaml``, schema ``runner-task.schema.yaml``) is the
implementer's EXCLUSIVE runtime interface. Historically it carried a file/verify load plan
but NO normative description of WHAT to build — objective, ordered steps, done-when
predicates, or the allowed change set. Under the live host-verify ENFORCE gate any source
diff + any zero-exit command reads as "done", so an underinformed runner could pass a task
without implementing it.

This module is:
  * the EMITTER — ``contract_fields_from_task`` maps an approved ``tasks.yaml`` task VERBATIM
    onto the packet's contract fields (it never INVENTS data), and ``apply_contract`` fills
    those fields onto a packet the dispatcher hands to a runner (packet-authored values win);
  * the VALIDATOR — ``validate_packet_contract`` checks a packet actually carries the
    contract, gated by ``BUILDER_PACKET_CONTRACT`` ('enforce' DEFAULT | 'off'), mirroring
    ``BUILDER_HOST_VERIFY``.

NON-BREAKING: the contract fields are OPTIONAL in ``runner-task.schema.yaml``; the emitter
FILLS them from the approved task where a packet omits them; and the hard requirement that
they be present is gated ON by default (``BUILDER_PACKET_CONTRACT=off`` => strict no-op,
byte-identical to prior behavior — no inspection, no rejection).
"""

from __future__ import annotations

from _dispatch_runtime.paths import RUNTIME_DIR_NAMES

import os
from typing import Any

# The contract fields a packet MUST carry to be self-describing under enforce.
REQUIRED_CONTRACT_FIELDS: tuple[str, ...] = (
    "objective",
    "steps",
    "done_when",
    "allowed_change_files",
)
# The full additive contract surface (all OPTIONAL in the schema).
CONTRACT_FIELDS: tuple[str, ...] = REQUIRED_CONTRACT_FIELDS + (
    "required_diff_classes",
    "acceptance_ids",
    "design_ids",
    "requirement_ids",
)

# tdd_mode -> the diff classes a faithful implementation is expected to touch. A behavior
# task (tdd required) must change BOTH production and test; the exempt_* variants declare a
# narrower, non-behavioral change surface. Unknown/absent -> the safe behavior default.
_TDD_DIFF_CLASSES: dict[str, list[str]] = {
    "required": ["production", "test"],
    "exempt_refactor_only": ["production"],
    "exempt_type_only": ["production"],
    "exempt_delete_only": ["deletion"],
    "exempt_config_only": ["config"],
    "exempt_infrastructure_only": ["config"],
    "exempt": ["production"],
}

_DECLARATION_HOME_PREFIX = ".builder-home"


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _str_list(value: Any) -> list[str]:
    """Coerce a YAML scalar/list to a list of non-empty strings, shape-safely: a bare string
    -> one element (NEVER iterated char-by-char); a list -> its stripped string/number items;
    None/other -> []."""
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(str(item))
        return out
    return []


def _present(value: Any) -> bool:
    """True when a contract field carries real content (not None / '' / empty list/dict)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def _normalize_allowed_path(value: str) -> list[str]:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return parts


def _is_forbidden_declaration_path(value: str) -> bool:
    parts = _normalize_allowed_path(value)
    if not parts:
        return False
    if parts[0] == _DECLARATION_HOME_PREFIX:
        parts = parts[1:]
    if not parts:
        return False
    if parts in (["builder.yaml"], ["repositories.yaml"], ["policy.yaml"]):
        return True
    if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "product.yaml":
        return True
    if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "releases":
        return True
    return False


def _task_steps(task: dict) -> list[str]:
    """The task's steps as verbatim text, from the canonical plan form (`steps: [{text: ...}]`)
    or a flat list of strings. Shape-safe; empties dropped."""
    steps = task.get("steps")
    if not isinstance(steps, (list, tuple)):
        return []
    out: list[str] = []
    for s in steps:
        if isinstance(s, dict):
            t = s.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
        elif isinstance(s, str) and s.strip():
            out.append(s.strip())
    return out


def _task_done_when(task: dict) -> list[str]:
    """The task's done_when predicate(s) as a list, VERBATIM. tasks.yaml declares a single
    string; a packet may already carry a list. Never split or invent structure."""
    dw = task.get("done_when")
    if isinstance(dw, str):
        s = dw.strip()
        return [s] if s else []
    return _str_list(dw)


def _task_acceptance_ids(task: dict) -> list[str]:
    """Acceptance-criterion ids the task proves: the task-level `proves` roll-up if present,
    else the union of per-verify `proves`. Verbatim; deduped, order-preserving."""
    ids = _str_list(task.get("proves"))
    if ids:
        return _dedup(ids)
    collected: list[str] = []
    verify = task.get("verify")
    if isinstance(verify, (list, tuple)):
        for entry in verify:
            if isinstance(entry, dict):
                collected += _str_list(entry.get("proves"))
    return _dedup(collected)


def _required_diff_classes(tdd_mode: str) -> list[str]:
    """Diff classes inferred from a (coarse or fine) tdd mode. Unknown/absent -> the behavior
    default [production, test] (the most-demanding, safe choice)."""
    key = (tdd_mode or "").strip().lower()
    return list(_TDD_DIFF_CLASSES.get(key, ["production", "test"]))


def contract_fields_from_task(
    task: Any, *, tdd_mode: str | None = None, links: dict | None = None
) -> dict:
    """THE EMITTER. Map an approved ``tasks.yaml`` task onto the packet contract fields,
    copying VERBATIM what the task declares (no invention):

      objective             <- task `title`
      steps                 <- task `steps[].text`
      done_when             <- task `done_when` (string -> 1-element list)
      allowed_change_files  <- task `files`
      acceptance_ids        <- task `proves` (or union of `verify[].proves`), or `links`
      required_diff_classes <- inferred from tdd mode (the finer `tdd_mode` override — the
                               packet's — when passed, else the task's own `tdd.mode`)
      requirement_ids /
      design_ids            <- from `links` (traceability-derived) when present

    Only NON-EMPTY fields are returned, so a caller merging this onto a packet never clobbers
    an authored value with an empty one. Shape-safe: a non-mapping task -> {}."""
    if not isinstance(task, dict):
        return {}
    links = links if isinstance(links, dict) else {}
    fields: dict[str, Any] = {}

    title = task.get("title")
    if isinstance(title, str) and title.strip():
        fields["objective"] = title.strip()

    steps = _task_steps(task)
    if steps:
        fields["steps"] = steps

    done_when = _task_done_when(task)
    if done_when:
        fields["done_when"] = done_when

    files = _str_list(task.get("files"))
    if files:
        fields["allowed_change_files"] = files

    # required_diff_classes: prefer an explicit (finer) tdd_mode override, else task.tdd.mode.
    resolved_mode = (tdd_mode or "").strip()
    if not resolved_mode:
        tdd = task.get("tdd")
        if isinstance(tdd, dict) and tdd.get("mode"):
            resolved_mode = str(tdd.get("mode")).strip()
    if resolved_mode:
        fields["required_diff_classes"] = _required_diff_classes(resolved_mode)

    acceptance = _str_list(links.get("acceptance_ids")) or _task_acceptance_ids(task)
    if acceptance:
        fields["acceptance_ids"] = _dedup(acceptance)

    design_ids = _str_list(links.get("design_ids"))
    if design_ids:
        fields["design_ids"] = _dedup(design_ids)

    requirement_ids = _str_list(links.get("requirement_ids"))
    if requirement_ids:
        fields["requirement_ids"] = _dedup(requirement_ids)

    return fields


def apply_contract(packet: Any, task: Any = None, *, links: dict | None = None) -> Any:
    """Return a shallow copy of ``packet`` with contract fields FILLED from the approved
    ``task`` wherever the packet omits (or leaves empty) them. Packet-authored values WIN
    (never clobbered). This is how the dispatcher POPULATES the runner's view of the contract
    from the plan/tasks — even for packets authored before the contract existed. The packet's
    finer ``tdd_mode`` (when present) drives ``required_diff_classes``. Shape-safe: a
    non-mapping packet is returned unchanged."""
    if not isinstance(packet, dict):
        return packet
    out = dict(packet)
    if not isinstance(task, dict):
        return out
    derived = contract_fields_from_task(
        task, tdd_mode=(str(packet.get("tdd_mode") or "").strip() or None), links=links
    )
    for key, value in derived.items():
        if not _present(out.get(key)):
            out[key] = value
    return out


def links_from_traceability(traceability: Any, task_id: str) -> dict:
    """Resolve a task's requirement/design ids from a ``traceability.yaml`` mapping:
    ``design_links`` (design_id -> task_ids) yields the task's design ids; ``requirement_links``
    (requirement_id -> design_ids) yields the requirements covering those designs. Verbatim;
    deduped. Shape-safe: {} on any malformed/missing structure."""
    if not isinstance(traceability, dict) or not task_id:
        return {}
    design_ids: list[str] = []
    for link in traceability.get("design_links") or []:
        if isinstance(link, dict) and str(task_id) in _str_list(link.get("task_ids")):
            did = link.get("design_id")
            if isinstance(did, str) and did.strip():
                design_ids.append(did.strip())
    design_ids = _dedup(design_ids)

    requirement_ids: list[str] = []
    if design_ids:
        dset = set(design_ids)
        for link in traceability.get("requirement_links") or []:
            if isinstance(link, dict) and dset.intersection(_str_list(link.get("design_ids"))):
                rid = link.get("requirement_id")
                if isinstance(rid, str) and rid.strip():
                    requirement_ids.append(rid.strip())

    out: dict[str, list[str]] = {}
    if requirement_ids:
        out["requirement_ids"] = _dedup(requirement_ids)
    if design_ids:
        out["design_ids"] = design_ids
    return out


def packet_contract_mode() -> str:
    """``BUILDER_PACKET_CONTRACT`` staging, mirroring ``BUILDER_HOST_VERIFY``: 'enforce'
    (default) | 'off'. This gate has no 'warn' tier, so an explicit 'warn' resolves to 'off'
    and is recorded as `abstain:off` -- non-blocking is what was asked for, and the evidence
    says so. An UNRECOGNIZED value resolves to the default (see `gate_mode`): a typo must
    never be the thing that quietly stops packets being checked."""
    from _dispatch_runtime.gate_evidence import gate_mode

    mode = gate_mode("BUILDER_PACKET_CONTRACT")
    return mode if mode in ("off", "enforce") else "off"


def missing_contract_fields(packet: Any) -> list[str]:
    """The REQUIRED contract fields absent/empty on ``packet`` (objective, steps, done_when,
    allowed_change_files), in declaration order. Shape-safe: a non-mapping packet is reported
    as missing all four."""
    if not isinstance(packet, dict):
        return list(REQUIRED_CONTRACT_FIELDS)
    return [f for f in REQUIRED_CONTRACT_FIELDS if not _present(packet.get(f))]


def validate_packet_contract(packet: Any) -> tuple[bool | None, str]:
    """Flag-gated packet-contract check. Returns (verdict, reason):

      * mode off (explicit opt-out) -> (None, "")  — strict no-op, no inspection.
      * enforce + complete -> (True, "")
      * enforce + missing  -> (False, reason naming the absent fields)

    Under enforce a packet must carry non-empty objective + steps + done_when +
    allowed_change_files (the normative description of WHAT to build); without them the runner
    would have to INFER the desired behavior from source files."""
    if packet_contract_mode() != "enforce":
        return None, ""
    missing = missing_contract_fields(packet)
    tid = packet.get("task_id") if isinstance(packet, dict) else None
    if isinstance(packet, dict):
        for entry in _str_list(packet.get("allowed_change_files")):
            normalized = entry.replace("\\", "/")
            if any(f"{name}/" in normalized for name in RUNTIME_DIR_NAMES) and "gate-evidence" in normalized:
                return False, f"allowed_change_files names a host-only gate-evidence path: {entry}"
            if _is_forbidden_declaration_path(entry):
                return False, f"allowed_change_files names a Builder Home declaration path: {entry}"
    if not missing:
        return True, ""
    who = f"task {tid}: " if tid else ""
    return False, f"{who}runner packet missing contract fields: {', '.join(missing)}"
