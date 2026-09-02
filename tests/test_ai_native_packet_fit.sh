#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

mkdir -p "$tmp_dir/.builder/specs/fixture"
mkdir -p "$tmp_dir/schemas"

cat > "$tmp_dir/schemas/runner.schema.yaml" <<'YAML'
type: object
properties:
  model_profiles:
    type: object
    properties:
      tiny_local:
        type: object
        effective_context_tokens: 12000
        initial_packet_cap_tokens: 4000
        max_full_read_files: 1
        max_slice_files: 3
        allow_rendered_markdown: false
YAML

cat > "$tmp_dir/.builder/specs/fixture/spec.yaml" <<'YAML'
name: fixture
created: 2026-05-20T19:20:00Z
status: planned
current_phase: 4-plan
next_action: validate packet fit
target_model_profile: tiny_local
YAML

cat > "$tmp_dir/.builder/specs/fixture/tasks.yaml" <<'YAML'
artifact: tasks
spec: fixture
tasks:
  - id: T1
    title: Compact packet
    repo: /path/to/project/
    files:
      - path: requirements.yaml
        mode: full
    tdd:
      mode: exempt
      reason: config-only
    steps: []
    verify:
      - command: echo ok
    done_when: done
YAML

cat > "$tmp_dir/.builder/specs/fixture/traceability.yaml" <<'YAML'
artifact: traceability
spec: fixture
requirement_links: []
design_links: []
task_links:
  - task_id: T1
    files:
      - path: requirements.yaml
        mode: full
        load_priority: must
        estimated_tokens: 100
      - path: requirements.md
        mode: full
        load_priority: must
        estimated_tokens: 100
    evidence_ids: []
YAML

PYTHONPATH="$repo_root/scripts" python3 - "$tmp_dir/.builder/specs/fixture" <<'PY'
from pathlib import Path
import sys

from _validators.common import ValidationContext
from _validators.packet_fit import run

context = ValidationContext(spec_dir=Path(sys.argv[1]))
result = run(context)
assert any("rendered markdown" in error.lower() for error in result.errors), result.errors
PY
