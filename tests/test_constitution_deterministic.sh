#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

mkdir -p "$tmp_dir/project/.builder" "$tmp_dir/.builder/specs/fixture"
cat > "$tmp_dir/project/.builder/constitution.yaml" <<'YAML'
artifact: constitution
project: fixture
source: .builder/constitution.md
principles:
  - id: no_core
    title: Do not create core layer
    severity: block
    rationale: Core layer is forbidden.
    applies_to:
      - 6-verify
    forbidden_paths:
      - src/core/**
YAML
cat > "$tmp_dir/project/.builder/constitution.md" <<'MD'
# Fixture Constitution
MD
cat > "$tmp_dir/.builder/specs/fixture/requirements.yaml" <<'YAML'
artifact: requirements
title: Fixture
spec: fixture
requirements:
  - id: R1
    title: Test
    user_story: As a tester, I want a fixture so that checks run.
    acceptance:
      - WHEN run, the system SHALL work.
YAML

set +e
PYTHONPATH="$repo_root/scripts" python3 "$repo_root/scripts/validate-constitution.py" fixture --root "$tmp_dir" --project-root "$tmp_dir/project" --strict --no-model --changed-files src/core/bad.ts >/tmp/constitution-fixture.out
status=$?
set -e
test "$status" -eq 1
grep -q "Constitution verdict: block" /tmp/constitution-fixture.out
test -f "$tmp_dir/.builder/specs/fixture/constitution-review.yaml"

set +e
PYTHONPATH="$repo_root/scripts" python3 "$repo_root/scripts/validate-constitution.py" fixture --root "$tmp_dir" --project-root "$tmp_dir/project" --strict --no-model --phase 3-review --changed-files src/core/bad.ts >/tmp/constitution-fixture-scoped.out
status=$?
set -e
test "$status" -eq 0
grep -q "Constitution verdict: pass" /tmp/constitution-fixture-scoped.out
