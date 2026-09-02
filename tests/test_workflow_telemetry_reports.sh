#!/bin/bash
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FIXTURE_ROOT="$ROOT/tests/fixtures/telemetry/report-fixture"

fail() {
  echo "FAIL: $1"
  exit 1
}

# The analyzer requires a literal <root>/.builder/telemetry/events tree. We keep the
# shipped fixtures OUTSIDE any '.builder' path (so nothing named .builder enters the
# public export) and materialize the .builder workspace in a temp dir at test time.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/.builder/telemetry/events"
cp -R "$FIXTURE_ROOT/events/." "$WORK/.builder/telemetry/events/"

REPORT_PATH="$WORK/.builder/telemetry/reports/telemetry-report.yaml"

set +e
output=$(python3 "$ROOT/scripts/analyze-workflow-telemetry.py" --root "$WORK" 2>&1)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "telemetry analysis command should succeed on fixture events. Output: $output"
fi
test -f "$REPORT_PATH" || fail "telemetry analysis should persist telemetry-report.yaml"
echo "PASS (1): telemetry analysis command writes telemetry-report.yaml"

set +e
output=$(WORK="$WORK" PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path

from _telemetry.aggregate import load_telemetry_report

report = load_telemetry_report(Path(os.environ["WORK"]) / ".builder/telemetry/reports/telemetry-report.yaml")
assert report["event_count"] == 6, report
assert report["summaries"]["validator_failures"] == 1, report
assert report["summaries"]["another_pass_loops"] == 1, report
assert report["summaries"]["archive_funnel"]["verified_events"] == 1, report
assert report["summaries"]["archive_funnel"]["archived_events"] == 1, report

command_counts = {item["command"]: item["count"] for item in report["summaries"]["command_usage"]}
assert command_counts["/isanna-5-implement"] == 2, command_counts
assert command_counts["/isanna-list"] == 1, command_counts

utility_counts = {item["command"]: item["count"] for item in report["summaries"]["utility_adoption"]}
assert utility_counts["/isanna-list"] == 1, utility_counts
assert utility_counts["/isanna-archive"] == 1, utility_counts

matrix = {(item["used_model"], item["outcome_category"]): item["count"] for item in report["summaries"]["model_outcome_matrix"]}
assert matrix[("GPT-5.4", "completed")] >= 2, matrix

print("report-ok")
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "telemetry report should expose expected rollups. Output: $output"
fi
echo "$output" | grep -q "report-ok" || fail "telemetry report verification should finish cleanly"
echo "PASS (2): telemetry report captures expected rollups"

set +e
output=$(WORK="$WORK" PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path

from _validators.common import load_schema, parse_yaml_like_file, validate_schema

report_path = Path(os.environ["WORK"]) / ".builder/telemetry/reports/telemetry-report.yaml"
data, errors = parse_yaml_like_file(report_path)
assert not errors, errors
schema, schema_errors = load_schema("telemetry-report.schema.yaml")
assert not schema_errors, schema_errors
errors = validate_schema(data, schema, report_path.name)
assert not errors, errors
print("schema-ok")
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "telemetry-report.yaml should validate against telemetry-report.schema.yaml. Output: $output"
fi
echo "$output" | grep -q "schema-ok" || fail "telemetry report schema validation should finish cleanly"
echo "PASS (3): telemetry report matches schema"

echo "ALL PASS: test_workflow_telemetry_reports.sh"
