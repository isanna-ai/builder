#!/bin/bash
# test_no_sp_leak.sh — RED/GREEN static guard for the /sp-* -> /isanna-* rebrand (W1).
#
# Asserts no shipped surface still tells a user (or an agent reading the shipped
# docs/prompts) to type a /sp-* slash command. Scope is deliberately the SHIPPED
# surface only: scripts, prompts, skills, standards, templates, README.md,
# install.sh. Historical/record directories (docs/planning, docs/archive,
# .builder, tests/fixtures) are excluded on purpose -- those are either
# superseded planning docs describing history, or -- for .builder -- a live
# flight recorder whose already-recorded command: history must NOT be
# rewritten (dual-accept parsing makes rewriting it unnecessary).
#
# ONE INTENTIONAL EXCEPTION inside the shipped scope: scripts/_telemetry/aggregate.py
# keeps literal "/sp-6-verify" and "/sp-archive" strings in its dual-accept counter
# sets ({"/sp-6-verify", "/isanna-6-verify"}) so historical /sp-* telemetry events
# still roll up correctly. That is backward-compat PARSING code, not a command
# surface told to a user -- exclude it explicitly rather than let it mask a real
# regression elsewhere in scripts/.
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1

fail() {
  echo "FAIL: $1"
  exit 1
}

output=$(git grep -nE '/sp-[a-z0-9]' -- \
  scripts \
  prompts \
  skills \
  standards \
  templates \
  README.md \
  install.sh \
  ':(exclude)scripts/_telemetry/aggregate.py' \
  2>/dev/null | grep -v '__pycache__')

if [ -n "$output" ]; then
  fail "shipped surface still references a /sp-* command (dual-accept parsing means it should say /isanna-* instead):
$output"
fi

echo "PASS: no /sp-* references remain in the shipped surface"
echo "test_no_sp_leak.sh PASS"
