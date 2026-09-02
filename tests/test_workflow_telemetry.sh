#!/bin/bash
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES="$ROOT/tests/fixtures/telemetry"
TMP_DIR="${TMPDIR:-/tmp}/builder-telemetry-$$"
export ROOT TMP_DIR

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

mkdir -p "$TMP_DIR"

set +e
output=$(PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path

from _telemetry.record import record_workflow_event, validate_workflow_event

root = Path(os.environ["ROOT"])
fixtures = root / "tests/fixtures/telemetry"
tmp_dir = Path(os.environ["TMP_DIR"]) / "record-test"
tmp_dir.mkdir(parents=True, exist_ok=True)

errors = validate_workflow_event(fixtures / "workflow-event-good.yaml")
assert not errors, errors
recorded_path = record_workflow_event(
    fixtures / "workflow-event-good.yaml",
    workspace_root=tmp_dir,
)
print(recorded_path)
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "workflow-event good fixture should validate and record. Output: $output"
fi
echo "$output" | grep -q "/events/2026-04-26/EVT-20260426-001.yaml" \
  || fail "workflow-event recorder should persist under .builder/telemetry/events/<date>/<id>.yaml. Output: $output"
test -f "$TMP_DIR/record-test/.builder/telemetry/events/2026-04-26/EVT-20260426-001.yaml" \
  || fail "workflow-event recorder should create the canonical event file"
echo "PASS (1): workflow-event good fixture validates and records"

set +e
output=$(PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path

from _telemetry.record import validate_workflow_event

errors = validate_workflow_event(Path(os.environ["ROOT"]) / "tests/fixtures/telemetry/workflow-event-bad-missing-outcome.yaml")
assert errors, "expected missing outcome error"
print("\n".join(errors))
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "missing-outcome fixture should produce validation errors without crashing. Output: $output"
fi
echo "$output" | grep -q 'missing required field `outcome_category`' \
  || fail "workflow-event schema should require outcome_category. Output: $output"
echo "PASS (2): workflow-event rejects missing outcome_category"

set +e
output=$(PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path

from _telemetry.record import validate_workflow_event

errors = validate_workflow_event(Path(os.environ["ROOT"]) / "tests/fixtures/telemetry/workflow-event-bad-estimated-tokens.yaml")
assert errors, "expected estimated-tokens error"
print("\n".join(errors))
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "estimated-tokens fixture should produce validation errors without crashing. Output: $output"
fi
echo "$output" | grep -Eq 'input_tokens|expected integer' \
  || fail "workflow-event schema should reject string token counts. Output: $output"
echo "PASS (3): workflow-event rejects estimated token strings"

grep -q 'workflow-event' "$ROOT/prompts/isanna-1-specify.prompt.md" \
  || fail "/isanna-1-specify prompt should document workflow-event telemetry"
grep -q 'reason_category' "$ROOT/prompts/isanna-5-implement.prompt.md" \
  || fail "/isanna-5-implement prompt should document reason_category telemetry"
grep -q 'execution_path' "$ROOT/prompts/isanna-6-verify.prompt.md" \
  || fail "/isanna-6-verify prompt should document execution_path telemetry"
grep -q 'thinking_effort' "$ROOT/prompts/isanna-ff.prompt.md" \
  || fail "/isanna-ff prompt should document thinking_effort telemetry"
# isanna-telemetry is a pure CLI utility, so it deliberately has no prompt file.
grep -q 'outcome_category' "$ROOT/prompts/isanna-archive.prompt.md" \
  || fail "/isanna-archive prompt should document outcome_category telemetry"
grep -q 'reason_category' "$ROOT/prompts/isanna-debug.prompt.md" \
  || fail "/isanna-debug prompt should document reason_category telemetry"
grep -q 'reason_category' "$ROOT/prompts/isanna-setup.prompt.md" \
  || fail "/isanna-setup prompt should document reason_category telemetry"
grep -q 'workflow-event' "$ROOT/standards/builder-contract.md" \
  || fail "builder-contract.md should document workflow-event artifacts"
grep -q 'telemetry-report' "$ROOT/standards/builder-workflow.md" \
  || fail "builder-workflow.md should document telemetry-report artifacts"
# The README must document the telemetry aggregator by its REAL invocation. It used to be
# asserted here as `/isanna-telemetry`, which pinned an inaccuracy: there is no such prompt file
# (this test says so three lines up), and the README itself defines CLI utilities as having no
# slash command. A test that requires the docs to be wrong is worse than no test.
grep -q 'analyze-workflow-telemetry.py' "$ROOT/README.md" \
  || fail "README.md should document the telemetry aggregator's real invocation"
if grep -q '/isanna-telemetry' "$ROOT/README.md"; then
  fail "README.md must not present the telemetry aggregator as a slash command -- it has no prompt file"
fi
echo "PASS (4): prompt and doc surfaces expose workflow telemetry"

if grep -RInE 'estimate token|estimate tokens|approx.*token|count tokens' "$ROOT/prompts/isanna-1-specify.prompt.md" "$ROOT/prompts/isanna-2-design.prompt.md" "$ROOT/prompts/isanna-3-review.prompt.md" "$ROOT/prompts/isanna-4-plan.prompt.md" "$ROOT/prompts/isanna-5-implement.prompt.md" "$ROOT/prompts/isanna-6-verify.prompt.md" "$ROOT/prompts/isanna-ff.prompt.md" >/dev/null 2>&1; then
  fail "lifecycle prompts must not tell agents to estimate token counts"
fi
echo "PASS (5): lifecycle prompts forbid token estimation"

set +e
output=$(PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
import os
from pathlib import Path
import sys

from _telemetry.common import sanitize_telemetry_payload, apply_retention_policy
from _telemetry.record import record_workflow_event, validate_workflow_event

fixtures = Path(os.environ["ROOT"]) / "tests/fixtures/telemetry"
data, _ = __import__("_validators.common", fromlist=["parse_yaml_like_file"]).parse_yaml_like_file(fixtures / "workflow-event-bad-sensitive-payload.yaml")

sanitized = sanitize_telemetry_payload(data)
assert "raw_prompt" not in sanitized, "raw_prompt must be dropped"
assert "sk-1234567890abcdefABCDEF" not in sanitized["intent_summary"], "secret must be redacted"  # publish-ok: deliberate redaction fixture
assert "[redacted]" in sanitized["intent_summary"], "redaction marker missing"
fields = sanitized["redaction"]["fields"]
assert any(f.startswith("intent_summary:") for f in fields), f"redaction.fields missing intent_summary tag: {fields}"
assert any(f.startswith("raw_prompt:") for f in fields), f"redaction.fields missing raw_prompt tag: {fields}"

# truncation case
data2 = dict(data)
data2["intent_summary"] = "A" * 250
sanitized2 = sanitize_telemetry_payload(data2)
assert len(sanitized2["intent_summary"]) <= 200, "intent_summary must be capped at 200 chars"

# raw code block case
data3 = dict(data)
data3["intent_summary"] = "Wrote ```python\nsecret_code\n``` block"
sanitized3 = sanitize_telemetry_payload(data3)
assert "```" not in sanitized3["intent_summary"], "raw code fences must be redacted"

print("REDACT_OK")
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "sanitize_telemetry_payload should redact secrets, raw code, and drop raw prompts. Output: $output"
fi
echo "$output" | grep -q "REDACT_OK" || fail "redaction sanity check did not run. Output: $output"
echo "PASS (6): sanitize_telemetry_payload redacts secrets, raw code, and drops raw prompts"

set +e
output=$(PYTHONPATH="$ROOT/scripts" python3 - <<'PY' 2>&1
from datetime import datetime
import os
from pathlib import Path

from _telemetry.common import apply_retention_policy

root = Path(os.environ["TMP_DIR"]) / "retention-test"
events = root / ".builder/telemetry/events"
old = events / "2025-01-01"
recent = events / "2026-04-25"
old.mkdir(parents=True, exist_ok=True)
recent.mkdir(parents=True, exist_ok=True)
(old / "EVT-OLD.yaml").write_text("artifact: workflow-event\n", encoding="utf-8")
(recent / "EVT-NEW.yaml").write_text("artifact: workflow-event\n", encoding="utf-8")

removed = apply_retention_policy(root, max_age_days=90, now=datetime(2026, 4, 26))
assert any("EVT-OLD" in str(p) for p in removed), f"old event not removed: {removed}"
assert not (old / "EVT-OLD.yaml").exists(), "old event file should be deleted"
assert (recent / "EVT-NEW.yaml").exists(), "recent event file must be retained"
print("RETENTION_OK")
PY
)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "apply_retention_policy should delete events older than max_age_days. Output: $output"
fi
echo "$output" | grep -q "RETENTION_OK" || fail "retention sanity check did not run. Output: $output"
rm -rf "$TMP_DIR/retention-test"
echo "PASS (7): apply_retention_policy deletes events older than retention window"

grep -q 'redact\|retention' "$ROOT/standards/builder-standards.md" \
  || fail "standards/builder-standards.md should document telemetry redaction and retention"
echo "PASS (8): standards documents telemetry redaction and retention"

echo "ALL PASS: test_workflow_telemetry.sh"
