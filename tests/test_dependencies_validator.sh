#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
validator="$repo_root/scripts/validate-spec.py"
tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/isanna-dependencies-validator.XXXXXX")"
trap 'rm -rf "$tmp_root"' EXIT
specs_root="$tmp_root/.builder/specs"

rm -rf "$tmp_root"
mkdir -p "$specs_root"
mkdir -p "$tmp_root/prompts"

write_spec() {
  local dir="$1"
  local name="$2"
  mkdir -p "$dir"
  cat > "$dir/spec.yaml" <<EOF
name: $name
created: 2026-04-27T00:00:00Z
status: specifying
current_phase: 1-specify
next_action: "fixture"
EOF

  cat > "$dir/system-model.yaml" <<'EOF'
version: 1
what:
  entities: []
  capabilities: []
who:
  actors: []
when:
  events: []
where:
  boundaries: []
why:
  rules: []
how:
  behaviors: []
upstream:
  sources: []
downstream:
  sinks: []
EOF
}

write_deps() {
  local dir="$1"
  local body="$2"
  cat > "$dir/dependencies.yaml" <<EOF
artifact: dependencies
spec: $(basename "$dir")
dependencies:
$body
EOF
}

write_spec "$specs_root/dep-target" "dep-target"
write_spec "$specs_root/good-spec" "good-spec"
write_spec "$specs_root/missing-target" "missing-target"
write_spec "$specs_root/duplicate-target" "duplicate-target"
write_spec "$specs_root/self-target" "self-target"

write_deps "$specs_root/good-spec" "  - spec: dep-target
    kind: required
    reason: shared prerequisite"

write_deps "$specs_root/missing-target" "  - spec: does-not-exist
    kind: required
    reason: broken edge"

write_deps "$specs_root/duplicate-target" "  - spec: dep-target
    kind: required
    reason: first edge
  - spec: dep-target
    kind: contextual
    reason: duplicate edge"

write_deps "$specs_root/self-target" "  - spec: self-target
    kind: required
    reason: invalid self edge"

python3 "$validator" --list-checks | grep -q '^dependencies$'

python3 "$validator" --strict "$specs_root/good-spec" >/tmp/builder-deps-good.out

if python3 "$validator" --strict "$specs_root/missing-target" >/tmp/builder-deps-missing.out 2>&1; then
  echo "expected missing-target validation to fail" >&2
  exit 1
fi
grep -qi 'unknown\|does-not-exist' /tmp/builder-deps-missing.out

if python3 "$validator" --strict "$specs_root/duplicate-target" >/tmp/builder-deps-duplicate.out 2>&1; then
  echo "expected duplicate-target validation to fail" >&2
  exit 1
fi
grep -qi 'duplicate' /tmp/builder-deps-duplicate.out

if python3 "$validator" --strict "$specs_root/self-target" >/tmp/builder-deps-self.out 2>&1; then
  echo "expected self-target validation to fail" >&2
  exit 1
fi
grep -qi 'self' /tmp/builder-deps-self.out

echo "test_dependencies_validator.sh PASS"
