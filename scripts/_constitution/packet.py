from __future__ import annotations

from pathlib import Path


def build_spec_text(spec_dir: Path) -> str:
    parts: list[str] = []
    for name in ("requirements.yaml", "design.yaml", "tasks.yaml", "traceability.yaml"):
        path = spec_dir / name
        if path.is_file():
            parts.append(f"# {name}\n{path.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(parts)

