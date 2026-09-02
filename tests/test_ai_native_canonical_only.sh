#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

mkdir -p "$tmp_dir/.builder/specs/fixture"

cat > "$tmp_dir/.builder/specs/fixture/spec.yaml" <<'YAML'
name: fixture
created: 2026-05-20T19:20:00Z
status: planned
current_phase: 4-plan
next_action: validate canonical only
artifact_mode: ai_native
YAML

cat > "$tmp_dir/.builder/specs/fixture/requirements.yaml" <<'YAML'
artifact: requirements
title: Fixture
spec: fixture
requirements:
  - id: R1
    title: Canonical only
    user_story: As an agent, I want canonical artifacts so that markdown is optional.
    acceptance:
      - WHEN validation runs, the system SHALL accept missing rendered Markdown in ai_native mode.
YAML

PYTHONPATH="$repo_root/scripts" python3 - "$tmp_dir/.builder/specs/fixture" <<'PY'
from pathlib import Path
import sys

from _validators.common import ValidationContext
from _validators.requirements import run

context = ValidationContext(spec_dir=Path(sys.argv[1]))
result = run(context)
assert not result.errors, result.errors

(context.spec_dir / "spec.yaml").write_text(
    (context.spec_dir / "spec.yaml").read_text(encoding="utf-8").replace("ai_native", "dual"),
    encoding="utf-8",
)
dual_result = run(context)
assert any("rendered view" in error for error in dual_result.errors), dual_result.errors
PY
