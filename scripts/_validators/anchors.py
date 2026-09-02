from __future__ import annotations

import re
from pathlib import Path

from .runtime import RUNTIME_DIR_NAMES

from .common import CheckResult, ValidationContext, parse_yaml_like_file


def _root_from_spec(spec_dir: Path) -> Path:
    if len(spec_dir.parts) >= 3 and spec_dir.parts[-3] in RUNTIME_DIR_NAMES and spec_dir.parts[-2] == "specs":
        return spec_dir.parents[2]
    return spec_dir.parent


def _resolve(spec_dir: Path, rel: str) -> Path | None:
    for base in (spec_dir, _root_from_spec(spec_dir)):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def _matches(text: str, kind: str, locator: str) -> bool:
    if kind == "literal_string":
        return locator in text
    if kind == "regex_v1":
        return re.search(locator, text, flags=re.MULTILINE) is not None
    if kind == "symbol_v1":
        return re.search(rf"\b{re.escape(locator)}\b", text) is not None
    return False


def run(context: ValidationContext) -> CheckResult:
    trace_path = context.spec_dir / "traceability.yaml"
    if not trace_path.exists():
        return CheckResult("anchors", [], skipped=True, skip_message=f"traceability.yaml not found at {trace_path}")
    data, parse_errors = parse_yaml_like_file(trace_path)
    errors = list(parse_errors)
    task_links = data.get("task_links") if isinstance(data.get("task_links"), list) else []
    checks = 0
    for link in task_links:
        if not isinstance(link, dict):
            continue
        files = link.get("files") if isinstance(link.get("files"), list) else []
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            path = str(file_entry.get("path", "")).strip()
            anchors = file_entry.get("anchors") if isinstance(file_entry.get("anchors"), list) else []
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    continue
                checks += 1
                anchor_id = str(anchor.get("id", "")).strip()
                resolved = _resolve(context.spec_dir, path)
                if resolved is None:
                    errors.append(f"anchor {anchor_id}: file not found at {path}")
                    continue
                text = resolved.read_text(encoding="utf-8", errors="replace")
                kind = str(anchor.get("kind", "")).strip()
                locator = str(anchor.get("locator", "")).strip()
                try:
                    ok = _matches(text, kind, locator)
                except re.error as exc:
                    errors.append(f"anchor {anchor_id}: invalid regex {exc}")
                    continue
                if not ok:
                    errors.append(f"anchor {anchor_id} (kind={kind}) resolves to zero hits in {path} - update locator or use +/-20-line fallback")
    return CheckResult("anchors", errors, total_checks=max(1, checks), summary=None if errors else "anchors: valid")
