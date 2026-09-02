from __future__ import annotations

from pathlib import Path
from typing import Any

import fnmatch

from _validators.common import parse_yaml_like_file


def adapter_for_repo(root: Path):
    path = root / ".builder" / "sync-adapter.yaml"
    data, errors = parse_yaml_like_file(path)
    if not errors and data.get("artifact") == "sync-adapter" and isinstance(data.get("mappings"), list):
        return BuilderSemanticAdapter(root, data["mappings"])
    return None


class BuilderSemanticAdapter:
    def __init__(self, root: Path, mappings: list[dict[str, Any]]):
        self.root = root
        self.mappings = mappings

    def observed_tuples(self, changed_paths: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in changed_paths:
            matched = False
            for mapping in self.mappings:
                patterns = mapping.get("paths") if isinstance(mapping, dict) else None
                tuples = mapping.get("tuples") if isinstance(mapping, dict) else None
                if not isinstance(patterns, list) or not isinstance(tuples, list):
                    continue
                if not any(isinstance(pattern, str) and fnmatch.fnmatch(path, pattern) for pattern in patterns):
                    continue
                matched = True
                for item in tuples:
                    if not isinstance(item, dict):
                        continue
                    row = {
                        "category": str(item.get("category", "")).strip(),
                        "target": str(item.get("target", "")).strip(),
                        "change": str(item.get("change", "")).strip(),
                    }
                    if row not in rows:
                        rows.append(row)
            if not matched:
                rows.append({"category": "capabilities", "target": f"unmapped:{path}", "change": "enrich"})
        return sorted(rows, key=lambda row: (row["category"], row["target"], row["change"]))
