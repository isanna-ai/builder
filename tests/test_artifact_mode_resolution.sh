#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

mkdir -p "$tmp_dir/.builder/specs/fixture"

cat > "$tmp_dir/.builder/setup-decisions.yaml" <<'YAML'
schema_version: 1
workspace:
  roots:
    - /path/to/project
commands:
  default:
    test: deno test
    check: deno task check
boundaries:
  off_limits: []
validation: {}
discovered: {}
default_artifact_mode: ai_native
YAML

cat > "$tmp_dir/.builder/specs/fixture/setup-decisions.yaml" <<'YAML'
schema_version: 1
workspace:
  roots:
    - /path/to/project
commands:
  default:
    test: deno test
    check: deno task check
boundaries:
  off_limits: []
validation: {}
discovered: {}
artifact_mode: dual
YAML

cat > "$tmp_dir/.builder/specs/fixture/spec.yaml" <<'YAML'
name: fixture
created: 2026-05-20T19:20:00Z
status: planned
current_phase: 4-plan
next_action: resolve artifact mode
YAML

PYTHONPATH="$repo_root/scripts" python3 - "$tmp_dir/.builder/specs/fixture" <<'PY'
from pathlib import Path
import sys

from _validators.common import ValidationContext, resolve_artifact_mode

spec_dir = Path(sys.argv[1])
context = ValidationContext(spec_dir=spec_dir)

assert resolve_artifact_mode(context) == "dual"

(spec_dir / "spec.yaml").write_text(
    (spec_dir / "spec.yaml").read_text(encoding="utf-8") + "artifact_mode: ai_native\n",
    encoding="utf-8",
)
assert resolve_artifact_mode(context) == "ai_native"

(spec_dir / "spec.yaml").write_text(
    (spec_dir / "spec.yaml").read_text(encoding="utf-8").replace("artifact_mode: ai_native\n", ""),
    encoding="utf-8",
)
(spec_dir / "setup-decisions.yaml").write_text(
    (spec_dir / "setup-decisions.yaml").read_text(encoding="utf-8").replace("artifact_mode: dual", "default_artifact_mode: dual"),
    encoding="utf-8",
)
assert resolve_artifact_mode(context) == "dual"

(spec_dir / "setup-decisions.yaml").write_text(
    (spec_dir / "setup-decisions.yaml").read_text(encoding="utf-8").replace("default_artifact_mode: dual", "default_artifact_mode: ai_native"),
    encoding="utf-8",
)
assert resolve_artifact_mode(context) == "ai_native"

(spec_dir / "setup-decisions.yaml").unlink()
(spec_dir.parent.parent / "setup-decisions.yaml").unlink()
assert resolve_artifact_mode(context) == "dual"
PY
