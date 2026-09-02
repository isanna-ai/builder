#!/bin/bash
set -o pipefail

BUILDER_SRC=$(cd "$(dirname "$0")/.." && pwd)
INSTALL_SH="$BUILDER_SRC/install.sh"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/test-installer-assets.XXXXXX")

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

target="$TMP_DIR/install-root"
mkdir -p "$target/.git"

set +e
output=$(/bin/sh "$INSTALL_SH" --target "$target" --yes 2>&1)
code=$?
set -e

if [ "$code" != "0" ]; then
  fail "install.sh should succeed for installer-assets regression. Output:\n$output"
fi

for template in spec.yaml intent-object.yaml requirements.yaml design.yaml tasks.yaml handoff.yaml setup-decisions.yaml; do
  test -f "$target/.builder/templates/$template" || \
    fail "installer should copy .builder/templates/$template. Output:\n$output"
done

# isanna-telemetry is intentionally CLI-only; prompts/isanna-help.prompt.md directs users
# to analyze-workflow-telemetry.py rather than a nonexistent prompt asset.

for script in record-workflow-event.py analyze-workflow-telemetry.py; do
  test -f "$target/.builder/scripts/$script" || \
    fail "installer should copy .builder/scripts/$script. Output:\n$output"
done

for helper in __init__.py aggregate.py common.py record.py; do
  test -f "$target/.builder/scripts/_telemetry/$helper" || \
    fail "installer should copy .builder/scripts/_telemetry/$helper. Output:\n$output"
done

for schema in intent-object.schema.yaml workflow-event.schema.yaml telemetry-report.schema.yaml; do
  test -f "$target/.builder/schemas/$schema" || \
    fail "installer should copy .builder/schemas/$schema. Output:\n$output"
done

grep -q '".builder/templates/tasks.yaml"' "$target/.builder/install-state.json" || \
  fail "install-state.json should include .builder/templates/tasks.yaml"
grep -q '".builder/templates/intent-object.yaml"' "$target/.builder/install-state.json" || \
  fail "install-state.json should include .builder/templates/intent-object.yaml"
grep -q '".builder/scripts/analyze-workflow-telemetry.py"' "$target/.builder/install-state.json" || \
  fail "install-state.json should include .builder/scripts/analyze-workflow-telemetry.py"
grep -q '".builder/scripts/_telemetry/record.py"' "$target/.builder/install-state.json" || \
  fail "install-state.json should include .builder/scripts/_telemetry/record.py"

echo "PASS (1): installer ships telemetry assets and canonical YAML templates"
echo "ALL PASS: test_installer_assets.sh"
