#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
prompt="$ROOT/prompts/isanna-ff.prompt.md"
workflow="$ROOT/standards/builder-workflow.md"

! grep -q 'Ask the user to confirm or override' "$prompt"
! grep -q 'Get approval\.' "$prompt"
grep -q 'continue from that phase without asking the user to confirm' "$prompt"
grep -q 'fast-forward mode itself is the user' "$workflow"
grep -q 'do not pause fast-forward execution' "$workflow"

echo "test_fast_forward_prompt.sh PASS"
