#!/bin/bash
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RENDERER="python3 $ROOT/scripts/render-spec-artifacts.py"
VALIDATOR="python3 $ROOT/scripts/validate-spec.py"
FIXTURES="$ROOT/tests/fixtures/structured"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/isanna-structured-root.XXXXXX")
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/isanna-structured-artifacts.XXXXXX")
trap 'rm -rf "$TMP_ROOT" "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

mkdir -p "$TMP_ROOT/prompts" "$TMP_ROOT/.builder/intents/fixture"
cat > "$TMP_ROOT/.builder/intents/fixture/intent.yaml" <<'EOF'
name: fixture
specs:
  - spec-under-test
EOF
TMP_SPEC="$TMP_ROOT/.builder/specs/spec-under-test"

write_ssot_delta() {
  printf 'capabilities: []\nbehaviors: []\njourneys: []\n' > "$TMP_SPEC/ssot-delta.yaml"
}

TMP_RENDERED=$(mktemp "${TMPDIR:-/tmp}/rendered-structured-tasks.XXXXXX.md")
set +e
$RENDERER tasks "$FIXTURES/tasks.yaml" > "$TMP_RENDERED" 2>"$TMP_RENDERED.stderr"
code=$?
set -e
[ "$code" = "0" ] || fail "renderer should render tasks fixture. Output: $(cat "$TMP_RENDERED.stderr")"
diff -u "$FIXTURES/tasks.md" "$TMP_RENDERED" \
  || fail "renderer output should match the golden tasks.md fixture"
echo "PASS (1): renderer matches golden tasks.md"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/tasks.yaml" "$TMP_SPEC/tasks.yaml"
cp "$FIXTURES/tasks.md" "$TMP_SPEC/tasks.md"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-artifacts
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-artifacts"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: task_plan
      name: Task plan
  capabilities:
    - id: render_plan
      name: Render plan
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [render_plan]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: plan_rule
      statement: Canonical planning data lives in tasks.yaml.
      applies_to: [task_plan, render_plan]
how:
  behaviors:
    - capability: render_plan
      success: Planner writes tasks.yaml and tasks.md together.
      failures: [tasks.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept matching tasks.yaml/tasks.md. Output: $output"
echo "$output" | grep -q "OK     tasks.yaml: valid" \
  || fail "validator output should report tasks.yaml as valid. Output: $output"
echo "PASS (2): validator accepts matching canonical tasks artifacts"

echo "# drift" >> "$TMP_SPEC/tasks.md"
set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject tasks.md drift"
echo "$output" | grep -q "rendered view drift" \
  || fail "validator drift output should mention rendered view drift. Output: $output"
echo "PASS (3): validator rejects tasks.md drift"

cp "$FIXTURES/tasks.md" "$TMP_SPEC/tasks.md"
sed -i '0,/Lock the artifact contract/s//Lock the artifact contract NOW/' "$TMP_SPEC/tasks.yaml"

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject tasks.yaml drift when tasks.md is stale"
echo "$output" | grep -q "rendered view drift" \
  || fail "validator drift output should mention rendered view drift after YAML mutation. Output: $output"
echo "PASS (4): validator rejects stale rendered markdown after YAML mutation"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/tasks.md" "$TMP_SPEC/tasks.md"
cp "$ROOT/tests/fixtures/structured-bad/tasks-missing-fields.yaml" "$TMP_SPEC/tasks.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-artifacts-bad
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-artifacts-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: task_plan
      name: Task plan
  capabilities:
    - id: render_plan
      name: Render plan
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [render_plan]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: plan_rule
      statement: Canonical planning data lives in tasks.yaml.
      applies_to: [task_plan, render_plan]
how:
  behaviors:
    - capability: render_plan
      success: Planner writes tasks.yaml and tasks.md together.
      failures: [tasks.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject tasks.yaml missing required fields"
echo "$output" | grep -q "missing required field" \
  || fail "validator should report missing required fields for bad tasks.yaml. Output: $output"
echo "PASS (5): validator rejects tasks.yaml missing required fields"

TMP_REQUIREMENTS_RENDERED="$TMP_DIR/rendered-requirements.md"
set +e
$RENDERER requirements "$FIXTURES/requirements.yaml" > "$TMP_REQUIREMENTS_RENDERED" 2>"$TMP_REQUIREMENTS_RENDERED.stderr"
code=$?
set -e
[ "$code" = "0" ] || fail "renderer should render requirements fixture. Output: $(cat "$TMP_REQUIREMENTS_RENDERED.stderr")"
diff -u "$FIXTURES/requirements.md" "$TMP_REQUIREMENTS_RENDERED" \
  || fail "renderer output should match the golden requirements.md fixture"
echo "PASS (6): renderer matches golden requirements.md"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/requirements.yaml" "$TMP_SPEC/requirements.yaml"
cp "$FIXTURES/requirements.md" "$TMP_SPEC/requirements.md"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-requirements
created: 2026-04-26T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design structured-requirements"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: requirement_set
      name: Requirement set
  capabilities:
    - id: write_requirements
      name: Write requirements
who:
  actors:
    - id: specifier
      name: Specifier
      capabilities: [write_requirements]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: requirements_rule
      statement: Canonical requirements live in requirements.yaml.
      applies_to: [requirement_set, write_requirements]
how:
  behaviors:
    - capability: write_requirements
      success: Specifier writes requirements.yaml and renders requirements.md.
      failures: [requirements.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept matching requirements.yaml/requirements.md. Output: $output"
echo "$output" | grep -q "OK     requirements.yaml: valid" \
  || fail "validator output should report requirements.yaml as valid. Output: $output"
echo "PASS (7): validator accepts canonical requirements artifacts"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/requirements.md" "$TMP_SPEC/requirements.md"
cp "$ROOT/tests/fixtures/structured-bad/requirements-bad-ears.yaml" "$TMP_SPEC/requirements.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-requirements-bad
created: 2026-04-26T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design structured-requirements-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: requirement_set
      name: Requirement set
  capabilities:
    - id: write_requirements
      name: Write requirements
who:
  actors:
    - id: specifier
      name: Specifier
      capabilities: [write_requirements]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: requirements_rule
      statement: Canonical requirements live in requirements.yaml.
      applies_to: [requirement_set, write_requirements]
how:
  behaviors:
    - capability: write_requirements
      success: Specifier writes requirements.yaml and renders requirements.md.
      failures: [requirements.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject bad requirements.yaml"
echo "$output" | grep -q "user_story\|acceptance" \
  || fail "validator should report the malformed requirements artifact. Output: $output"
echo "PASS (8): validator rejects malformed requirements.yaml"

TMP_REVIEW_RENDERED="$TMP_DIR/rendered-review-log.md"
set +e
$RENDERER review-log "$FIXTURES/review-log.yaml" > "$TMP_REVIEW_RENDERED" 2>"$TMP_REVIEW_RENDERED.stderr"
code=$?
set -e
[ "$code" = "0" ] || fail "renderer should render review-log fixture. Output: $(cat "$TMP_REVIEW_RENDERED.stderr")"
diff -u "$FIXTURES/review-log.md" "$TMP_REVIEW_RENDERED" \
  || fail "renderer output should match the golden review-log.md fixture"
echo "PASS (6): renderer matches golden review-log.md"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/review-log.yaml" "$TMP_SPEC/review-log.yaml"
cp "$FIXTURES/review-log.md" "$TMP_SPEC/review-log.md"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-review-log
created: 2026-04-26T00:00:00Z
status: reviewed
current_phase: 3-review
next_action: "Run /isanna-4-plan structured-review-log"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: review_log
      name: Review log
  capabilities:
    - id: record_review
      name: Record review
who:
  actors:
    - id: reviewer
      name: Reviewer
      capabilities: [record_review]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: review_rule
      statement: Canonical review data lives in review-log.yaml.
      applies_to: [review_log, record_review]
how:
  behaviors:
    - capability: record_review
      success: Reviewer writes review-log.yaml and rendered review-log.md together.
      failures: [review-log.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept matching review-log.yaml/review-log.md. Output: $output"
echo "$output" | grep -q "OK     review-log.yaml: valid" \
  || fail "validator output should report review-log.yaml as valid. Output: $output"
echo "PASS (7): validator accepts matching canonical review artifacts"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/review-log.md" "$TMP_SPEC/review-log.md"
cp "$ROOT/tests/fixtures/structured-bad/review-log-missing-verdict.yaml" "$TMP_SPEC/review-log.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-review-log-bad
created: 2026-04-26T00:00:00Z
status: reviewed
current_phase: 3-review
next_action: "Run /isanna-4-plan structured-review-log-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: review_log
      name: Review log
  capabilities:
    - id: record_review
      name: Record review
who:
  actors:
    - id: reviewer
      name: Reviewer
      capabilities: [record_review]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: review_rule
      statement: Canonical review data lives in review-log.yaml.
      applies_to: [review_log, record_review]
how:
  behaviors:
    - capability: record_review
      success: Reviewer writes review-log.yaml and rendered review-log.md together.
      failures: [review-log.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject review-log.yaml missing verdict"
echo "$output" | grep -q "verdict" \
  || fail "validator should report the missing review verdict. Output: $output"
echo "PASS (10): validator rejects review-log.yaml missing verdict"

TMP_DESIGN_RENDERED="$TMP_DIR/rendered-design.md"
set +e
$RENDERER design "$FIXTURES/design.yaml" > "$TMP_DESIGN_RENDERED" 2>"$TMP_DESIGN_RENDERED.stderr"
code=$?
set -e
[ "$code" = "0" ] || fail "renderer should render design fixture. Output: $(cat "$TMP_DESIGN_RENDERED.stderr")"
diff -u "$FIXTURES/design.md" "$TMP_DESIGN_RENDERED" \
  || fail "renderer output should match the golden design.md fixture"
echo "PASS (11): renderer matches golden design.md"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/design.yaml" "$TMP_SPEC/design.yaml"
cp "$FIXTURES/design.md" "$TMP_SPEC/design.md"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-design
created: 2026-04-26T00:00:00Z
status: designed
current_phase: 2-design
next_action: "Run /isanna-3-review structured-design"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: design_doc
      name: Design doc
  capabilities:
    - id: write_design
      name: Write design
who:
  actors:
    - id: designer
      name: Designer
      capabilities: [write_design]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: design_rule
      statement: Canonical design data lives in design.yaml.
      applies_to: [design_doc, write_design]
how:
  behaviors:
    - capability: write_design
      success: Designer writes design.yaml and renders design.md.
      failures: [design.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept matching design.yaml/design.md. Output: $output"
echo "$output" | grep -q "OK     design.yaml: valid" \
  || fail "validator output should report design.yaml as valid. Output: $output"
echo "PASS (12): validator accepts canonical design artifacts"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/design.md" "$TMP_SPEC/design.md"
cp "$ROOT/tests/fixtures/structured-bad/design-missing-verification.yaml" "$TMP_SPEC/design.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-design-bad
created: 2026-04-26T00:00:00Z
status: designed
current_phase: 2-design
next_action: "Run /isanna-3-review structured-design-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: design_doc
      name: Design doc
  capabilities:
    - id: write_design
      name: Write design
who:
  actors:
    - id: designer
      name: Designer
      capabilities: [write_design]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: design_rule
      statement: Canonical design data lives in design.yaml.
      applies_to: [design_doc, write_design]
how:
  behaviors:
    - capability: write_design
      success: Designer writes design.yaml and renders design.md.
      failures: [design.md drift is not allowed]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject design.yaml missing verification_strategy"
echo "$output" | grep -q "verification_strategy" \
  || fail "validator should report the missing verification_strategy. Output: $output"
echo "PASS (13): validator rejects malformed design.yaml"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/setup-decisions.yaml" "$TMP_SPEC/setup-decisions.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-setup
created: 2026-04-26T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design structured-setup"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: setup_context
      name: Setup context
  capabilities:
    - id: capture_setup
      name: Capture setup
who:
  actors:
    - id: onboarder
      name: Onboarder
      capabilities: [capture_setup]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: setup_rule
      statement: Canonical setup data lives in setup-decisions.yaml.
      applies_to: [setup_context, capture_setup]
how:
  behaviors:
    - capability: capture_setup
      success: Onboarding records repo graph and command maps in setup-decisions.yaml.
      failures: [setup context is missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept setup-decisions.yaml matching the schema. Output: $output"
echo "$output" | grep -q "OK     setup-decisions.yaml: valid" \
  || fail "validator output should report setup-decisions.yaml as valid. Output: $output"
echo "PASS (14): validator accepts canonical setup-decisions data"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$ROOT/tests/fixtures/structured-bad/setup-missing-roots.yaml" "$TMP_SPEC/setup-decisions.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-setup-bad
created: 2026-04-26T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design structured-setup-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: setup_context
      name: Setup context
  capabilities:
    - id: capture_setup
      name: Capture setup
who:
  actors:
    - id: onboarder
      name: Onboarder
      capabilities: [capture_setup]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: setup_rule
      statement: Canonical setup data lives in setup-decisions.yaml.
      applies_to: [setup_context, capture_setup]
how:
  behaviors:
    - capability: capture_setup
      success: Onboarding records repo graph and command maps in setup-decisions.yaml.
      failures: [setup context is missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject setup-decisions.yaml missing roots"
echo "$output" | grep -q "workspace.roots\|boundaries.off_limits" \
  || fail "validator should report the malformed setup-decisions artifact. Output: $output"
echo "PASS (15): validator rejects malformed setup-decisions.yaml"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/validate-report.yaml" "$TMP_SPEC/validate-report.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-utility-report
created: 2026-04-26T00:00:00Z
status: reviewed
current_phase: 6-verify
next_action: "Run /isanna-archive structured-utility-report"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: utility_report
      name: Utility report
  capabilities:
    - id: emit_report
      name: Emit report
who:
  actors:
    - id: validator
      name: Validator
      capabilities: [emit_report]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: utility_rule
      statement: Structured utility output lives in <command>-report.yaml.
      applies_to: [utility_report, emit_report]
how:
  behaviors:
    - capability: emit_report
      success: Utility commands persist a canonical report before rendering the chat summary.
      failures: [utility report fields are missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept a complete utility report. Output: $output"
echo "$output" | grep -q "utility reports valid: validate-report.yaml" \
  || fail "validator output should report validate-report.yaml as valid. Output: $output"
echo "PASS (16): validator accepts canonical utility reports"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$ROOT/tests/fixtures/structured-bad/sync-report-missing-mode.yaml" "$TMP_SPEC/sync-report.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-utility-report-bad
created: 2026-04-26T00:00:00Z
status: reviewed
current_phase: 6-verify
next_action: "Run /isanna-archive structured-utility-report-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: utility_report
      name: Utility report
  capabilities:
    - id: emit_report
      name: Emit report
who:
  actors:
    - id: validator
      name: Validator
      capabilities: [emit_report]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: utility_rule
      statement: Structured utility output lives in <command>-report.yaml.
      applies_to: [utility_report, emit_report]
how:
  behaviors:
    - capability: emit_report
      success: Utility commands persist a canonical report before rendering the chat summary.
      failures: [utility report fields are missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject a utility report missing mode"
echo "$output" | grep -q "mode" \
  || fail "validator should report the missing utility report mode. Output: $output"
echo "PASS (17): validator rejects malformed utility reports"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC/evidence"
write_ssot_delta
cp "$FIXTURES/requirements.yaml" "$TMP_SPEC/requirements.yaml"
cp "$FIXTURES/requirements.md" "$TMP_SPEC/requirements.md"
cp "$FIXTURES/design.yaml" "$TMP_SPEC/design.yaml"
cp "$FIXTURES/design.md" "$TMP_SPEC/design.md"
cp "$FIXTURES/tasks.yaml" "$TMP_SPEC/tasks.yaml"
cp "$FIXTURES/tasks.md" "$TMP_SPEC/tasks.md"
cp "$FIXTURES/traceability.yaml" "$TMP_SPEC/traceability.yaml"
cp "$FIXTURES/evidence-task-1.yaml" "$TMP_SPEC/evidence/task-1.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-traceability
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-traceability"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: traceability_graph
      name: Traceability graph
  capabilities:
    - id: link_artifacts
      name: Link artifacts
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [link_artifacts]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: traceability_rule
      statement: Canonical traceability data links requirements, design, tasks, files, and evidence ids.
      applies_to: [traceability_graph, link_artifacts]
how:
  behaviors:
    - capability: link_artifacts
      success: Planner writes explicit requirement_links, design_links, and task_links.
      failures: [traceability links are dangling]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept canonical traceability links. Output: $output"
echo "$output" | grep -q "OK     traceability.yaml: valid" \
  || fail "validator output should report traceability.yaml as valid. Output: $output"
echo "PASS (18): validator accepts canonical traceability links"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC/evidence"
write_ssot_delta
cp "$FIXTURES/requirements.yaml" "$TMP_SPEC/requirements.yaml"
cp "$FIXTURES/requirements.md" "$TMP_SPEC/requirements.md"
cp "$FIXTURES/design.yaml" "$TMP_SPEC/design.yaml"
cp "$FIXTURES/design.md" "$TMP_SPEC/design.md"
cp "$FIXTURES/tasks.yaml" "$TMP_SPEC/tasks.yaml"
cp "$FIXTURES/tasks.md" "$TMP_SPEC/tasks.md"
cp "$ROOT/tests/fixtures/structured-bad/traceability-dangling-id.yaml" "$TMP_SPEC/traceability.yaml"
cp "$FIXTURES/evidence-task-1.yaml" "$TMP_SPEC/evidence/task-1.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-traceability-bad
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-traceability-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: traceability_graph
      name: Traceability graph
  capabilities:
    - id: link_artifacts
      name: Link artifacts
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [link_artifacts]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: traceability_rule
      statement: Canonical traceability data links requirements, design, tasks, files, and evidence ids.
      applies_to: [traceability_graph, link_artifacts]
how:
  behaviors:
    - capability: link_artifacts
      success: Planner writes explicit requirement_links, design_links, and task_links.
      failures: [traceability links are dangling]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject dangling traceability references"
echo "$output" | grep -q "unknown requirement\|unknown design\|unknown task\|unknown evidence" \
  || fail "validator should report dangling traceability ids. Output: $output"
echo "PASS (19): validator rejects dangling traceability ids"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC/evidence"
write_ssot_delta
cp "$ROOT/tests/fixtures/structured-bad/evidence-missing-red.yaml" "$TMP_SPEC/evidence/task-1.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-evidence-bad
created: 2026-04-26T00:00:00Z
status: implementing
current_phase: 5-implement
next_action: "Run /isanna-6-verify structured-evidence-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: execution_evidence
      name: Execution evidence
  capabilities:
    - id: record_evidence
      name: Record evidence
who:
  actors:
    - id: implementer
      name: Implementer
      capabilities: [record_evidence]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: evidence_rule
      statement: TDD-required tasks must record RED, GREEN, and VERIFY in evidence/task-<id>.yaml.
      applies_to: [execution_evidence, record_evidence]
how:
  behaviors:
    - capability: record_evidence
      success: Implementer records per-task evidence files and links them from phase-log.yaml.
      failures: [red evidence is missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF
cat > "$TMP_SPEC/phase-log.yaml" <<'EOF'
phases:
  - phase: 5-implement
    batch: run-1
    task_range: "1"
    used_model: "test-model-1"
    outcome: batch-complete
    tasks:
      - task_id: T1
        status: done
        tdd:
          mode: required
        evidence_file: evidence/task-1.yaml
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject per-task evidence missing the RED step"
echo "$output" | grep -q "missing 'red' evidence step" \
  || fail "validator should report the missing RED evidence step. Output: $output"
echo "PASS (20): validator rejects per-task evidence missing the RED step"

TMP_HANDOFF_RENDERED="$TMP_DIR/rendered-handoff.txt"
set +e
$RENDERER handoff "$FIXTURES/handoff.yaml" > "$TMP_HANDOFF_RENDERED" 2>"$TMP_HANDOFF_RENDERED.stderr"
code=$?
set -e
[ "$code" = "0" ] || fail "renderer should render handoff fixture. Output: $(cat "$TMP_HANDOFF_RENDERED.stderr")"
diff -u "$FIXTURES/handoff.txt" "$TMP_HANDOFF_RENDERED" \
  || fail "renderer output should match the golden handoff fixture"
echo "PASS (21): renderer matches golden handoff text"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$FIXTURES/handoff.yaml" "$TMP_SPEC/handoff.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-handoff
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-handoff"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: handoff
      name: Handoff
  capabilities:
    - id: emit_handoff
      name: Emit handoff
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [emit_handoff]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: handoff_rule
      statement: Canonical handoff data lives in handoff.yaml.
      applies_to: [handoff, emit_handoff]
how:
  behaviors:
    - capability: emit_handoff
      success: Planner writes handoff.yaml before rendering the final handoff block.
      failures: [handoff fields are missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" = "0" ] || fail "validator should accept a complete handoff.yaml. Output: $output"
echo "$output" | grep -q "OK     handoff.yaml: valid" \
  || fail "validator output should report handoff.yaml as valid. Output: $output"
echo "PASS (22): validator accepts canonical handoff data"

rm -rf "$TMP_SPEC"
mkdir -p "$TMP_SPEC"
write_ssot_delta
cp "$ROOT/tests/fixtures/structured-bad/handoff-missing-next-command.yaml" "$TMP_SPEC/handoff.yaml"
cat > "$TMP_SPEC/spec.yaml" <<'EOF'
name: structured-handoff-bad
created: 2026-04-26T00:00:00Z
status: planned
current_phase: 4-plan
next_action: "Run /isanna-5-implement structured-handoff-bad"
EOF
cat > "$TMP_SPEC/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: handoff
      name: Handoff
  capabilities:
    - id: emit_handoff
      name: Emit handoff
who:
  actors:
    - id: planner
      name: Planner
      capabilities: [emit_handoff]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: handoff_rule
      statement: Canonical handoff data lives in handoff.yaml.
      applies_to: [handoff, emit_handoff]
how:
  behaviors:
    - capability: emit_handoff
      success: Planner writes handoff.yaml before rendering the final handoff block.
      failures: [handoff fields are missing]
upstream:
  sources: []
downstream:
  sinks: []
EOF

set +e
output="$($VALIDATOR --strict "$TMP_SPEC" 2>&1)"
code=$?
set -e
[ "$code" != "0" ] || fail "validator should reject handoff.yaml missing next_command"
echo "$output" | grep -q "next_command" \
  || fail "validator should report the missing handoff next_command. Output: $output"
echo "PASS (23): validator rejects handoff.yaml missing next_command"

echo "ALL PASS: test_structured_artifacts.sh"
