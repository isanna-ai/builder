#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

cat > "$tmp_dir/spec.yaml" <<'YAML'
name: fixture
created: 2026-05-20T19:20:00Z
status: planned
current_phase: 4-plan
next_action: validate artifact mode
artifact_mode: ai_native
YAML

PYTHONPATH="$repo_root/scripts" python3 - "$tmp_dir/spec.yaml" <<'PY'
from pathlib import Path
import sys

from _validators.legacy import validate_spec_yaml

path = Path(sys.argv[1])
assert not validate_spec_yaml(path, None, True)

path.write_text(path.read_text(encoding="utf-8").replace("ai_native", "markdown_only"), encoding="utf-8")
errors = validate_spec_yaml(path, None, True)
assert any("artifact_mode" in error for error in errors), errors
PY
