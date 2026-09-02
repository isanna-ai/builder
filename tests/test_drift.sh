#!/bin/bash
# test_drift.sh — RED/GREEN test for check-mirror-drift.py
# Assertions:
#   1. Clean install root exits 0 (no drift)
#   2. Appending a line to an installed prompt exits non-zero and output contains 'unsupported-divergence'
#   3. Missing install-state.json exits 2
set -o pipefail

BUILDER_SRC=$(cd "$(dirname "$0")/.." && pwd)
DRIFT_SCRIPT="python3 $BUILDER_SRC/scripts/check-mirror-drift.py"
DRIFT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/drift-test-root.XXXXXX")

# Fresh install
rm -rf "$DRIFT_ROOT"
mkdir -p "$DRIFT_ROOT/.git"
/bin/sh "$BUILDER_SRC/install.sh" --target "$DRIFT_ROOT" --yes >/dev/null 2>&1 \
  || { echo "FAIL: install.sh failed during setup"; exit 1; }

fail() {
  echo "FAIL: $1"
  exit 1
}

# ── Assertion 1: clean root exits 0 ──────────────────────────────────────────
set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$DRIFT_ROOT" 2>&1)
code=$?
set -e
[ "$code" = "0" ] || fail "clean root should exit 0, got $code. Output: $output"
echo "PASS (1): clean root exits 0"

# ── Assertion 2: modified prompt → unsupported-divergence ────────────────────
echo "" >> "$DRIFT_ROOT/.github/prompts/isanna-1-specify.prompt.md"
echo "# drift" >> "$DRIFT_ROOT/.github/prompts/isanna-1-specify.prompt.md"
set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$DRIFT_ROOT" 2>&1)
code=$?
set -e
[ "$code" != "0" ] || fail "modified install should exit non-zero, got 0. Output: $output"
echo "$output" | grep -q 'unsupported-divergence' \
  || fail "output should contain 'unsupported-divergence'. Got: $output"
echo "PASS (2): modified prompt exits non-zero with unsupported-divergence"

# ── Assertion 3: missing install-state.json → exit 2 with exact message ──────
mv "$DRIFT_ROOT/.builder/install-state.json" "$DRIFT_ROOT/.builder/install-state.json.bak"
set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$DRIFT_ROOT" 2>&1)
code=$?
set -e
mv "$DRIFT_ROOT/.builder/install-state.json.bak" "$DRIFT_ROOT/.builder/install-state.json"
[ "$code" = "2" ] || fail "missing install-state.json should exit 2, got $code. Output: $output"
expected="$DRIFT_ROOT/.builder/install-state.json: not found; run install.sh first"
[ "$output" = "$expected" ] \
  || fail "missing install-state.json should print exact contract message. Got: $output"
echo "PASS (3): missing install-state.json exits 2 with exact message"

# ── Assertion 4: every installed scripts/schemas/tests/standards/templates
#    file must be a registered install-state.json asset key (disk-vs-registered
#    parity — install.sh copies _dispatch_runtime/*.py and _sync/*.py but must
#    also register them, or future mirrors of those files are invisible to drift) ──
set +e
parity_output=$(python3 - "$DRIFT_ROOT" <<'PYEOF'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
state = json.loads((root / ".builder" / "install-state.json").read_text())
registered = set(state.get("assets", {}).keys())

unregistered = []
for subdir in ("scripts", "schemas", "tests", "standards", "templates"):
    base = root / ".builder" / subdir
    if not base.is_dir():
        continue
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = ".builder/" + path.relative_to(root / ".builder").as_posix()
        if rel not in registered:
            unregistered.append(rel)

if unregistered:
    print("\n".join(sorted(unregistered)))
    sys.exit(1)
sys.exit(0)
PYEOF
)
parity_code=$?
set -e
[ "$parity_code" = "0" ] \
  || fail "installed files unregistered in install-state.json.assets:\n$parity_output"
echo "PASS (4): every installed scripts/schemas/tests/standards/templates file is registered"

# ── Assertion 4b: an unavailable recorded digest + unreadable canonical must
#    classify as "unverifiable" (counted as drift), not silently "clean" —
#    fail-closed when integrity genuinely cannot be checked ───────────────────
UNVERIFIABLE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/drift-test-unverifiable.XXXXXX")
mkdir -p "$UNVERIFIABLE_ROOT/install/.builder/scripts" "$UNVERIFIABLE_ROOT/canonical/scripts"
echo "print('installed, no matching canonical')" > "$UNVERIFIABLE_ROOT/install/.builder/scripts/ghost.py"
set +e
classify_result=$(python3 - "$BUILDER_SRC/scripts/check-mirror-drift.py" "$UNVERIFIABLE_ROOT" <<'PYEOF'
import importlib.util
import sys
from pathlib import Path

drift_script, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, str(Path(drift_script).parent))
spec = importlib.util.spec_from_file_location("check_mirror_drift", drift_script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

classification, hint = mod.classify(
    ".builder/scripts/ghost.py",
    root / "install",
    root / "canonical",
    "unavailable",
    [],
    None,
)
print(classification)
sys.exit(0 if classification == "unverifiable" else 1)
PYEOF
)
classify_code=$?
set -e
rm -rf "$UNVERIFIABLE_ROOT"
[ "$classify_code" = "0" ] \
  || fail "unavailable recorded digest + unreadable canonical should classify unverifiable, got: $classify_result"
echo "PASS (4b): unavailable recorded digest + unreadable canonical classifies unverifiable"

# ── Assertion 5/6: claude layout (.claude/commands/isanna-*.md) resolves its
#    canonical source via install-state.json's recorded "source" field —
#    _resolve_canonical's path-shape inference only understands .github/* and
#    .builder/*, so a claude install used to false-positive canonical:missing ──
CLAUDE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/drift-test-claude.XXXXXX")
rm -rf "$CLAUDE_ROOT"
mkdir -p "$CLAUDE_ROOT/.git"
/bin/sh "$BUILDER_SRC/install.sh" --ai claude --target "$CLAUDE_ROOT" --yes >/dev/null 2>&1 \
  || fail "install.sh --ai claude failed during setup"

set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$CLAUDE_ROOT" 2>&1)
code=$?
set -e
[ "$code" = "0" ] || fail "clean claude-layout root should exit 0, got $code. Output: $output"
echo "$output" | grep -q 'canonical:missing\|missing' \
  && fail "clean claude-layout root should not show canonical:missing. Output: $output"
echo "PASS (5): claude-layout install (.claude/commands/*.md) exits 0 clean"

claude_cmd=$(find "$CLAUDE_ROOT/.claude/commands" -name 'isanna-1-specify.md' | head -1)
[ -n "$claude_cmd" ] || fail "expected .claude/commands/isanna-1-specify.md to exist"
echo "" >> "$claude_cmd"
echo "# drift" >> "$claude_cmd"
set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$CLAUDE_ROOT" 2>&1)
code=$?
set -e
[ "$code" != "0" ] || fail "modified claude command should exit non-zero, got 0. Output: $output"
echo "$output" | grep -q 'unsupported-divergence' \
  || fail "modified claude command should report unsupported-divergence (source-resolved canonical). Got: $output"
echo "PASS (6): modified claude command exits non-zero with unsupported-divergence"
rm -rf "$CLAUDE_ROOT"

# ── Assertion 7: an allow-pattern match must not hide an ABSENT file — the
#    allowlist check must run AFTER the exists check, so a missing file that
#    happens to match an --allow-extension pattern still reports
#    missing-installed instead of silently passing as supported-extension ──
rm -f "$DRIFT_ROOT/.builder/scripts/list-specs.py"
set +e
output=$($DRIFT_SCRIPT --canonical "$BUILDER_SRC" --install-root "$DRIFT_ROOT" \
  --allow-extension 'list-specs.py' 2>&1)
code=$?
set -e
[ "$code" != "0" ] || fail "an absent allow-matched file should still exit non-zero, got 0. Output: $output"
echo "$output" | grep -q 'missing-installed' \
  || fail "an absent allow-matched file should report missing-installed, not be hidden. Got: $output"
echo "$output" | grep -q 'supported-extension.*list-specs.py' \
  && fail "an absent file must not be reported as supported-extension. Got: $output"
echo "PASS (7): an absent allow-pattern-matched file still reports missing-installed"
cp "$BUILDER_SRC/scripts/list-specs.py" "$DRIFT_ROOT/.builder/scripts/list-specs.py"

echo "ALL PASS: test_drift.sh"
