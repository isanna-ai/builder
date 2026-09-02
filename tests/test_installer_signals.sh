#!/usr/bin/env bash
# An interrupted install must ABORT, not finish and claim success.
#
# This existed as a bug for two releases: `trap cleanup EXIT HUP INT TERM` pointed the signal
# traps at a handler that only removes the staging directory and never exits. A trap handler that
# does not exit RESUMES the script -- so a signal during `curl | bash -s -- --yes` was swallowed,
# the install ran to completion, and the success banner printed. Measured before the fix:
# rc=0, banner printed, all files written. After: rc=143, no banner.
#
# SIGTERM, not SIGINT: a POSIX shell sets SIGINT to ignore for background jobs, so a SIGINT test
# run this way proves nothing. That subtlety is why the first attempt at this check passed
# against BOTH the fixed and the broken installer.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

target="$TMP/target"
mkdir -p "$target"
git -C "$target" init -q
git -C "$target" config user.email t@t.c
git -C "$target" config user.name t

log="$TMP/install.log"
sh "$ROOT/install.sh" --target "$target" --yes >"$log" 2>&1 &
pid=$!
sleep 0.6
kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
rc=$?

[ "$rc" -ne 0 ] || fail "an interrupted install exited 0; a swallowed signal makes Ctrl-C a no-op"
if grep -q "installed for" "$log"; then
  fail "an interrupted install printed the success banner"
fi
if ls -d "$target"/.builder-staging-* >/dev/null 2>&1; then
  fail "an interrupted install left a staging directory in the user's repo"
fi
echo "PASS (1): an interrupted install aborts (rc=$rc), prints no success banner, leaves no staging dir"

# And the ordinary path must be untouched by the trap changes.
clean="$TMP/clean"
mkdir -p "$clean"
git -C "$clean" init -q
sh "$ROOT/install.sh" --target "$clean" --yes >/dev/null 2>&1 || fail "a normal install no longer succeeds"
[ -f "$clean/.builder/standards/builder-contract.md" ] || fail "a normal install wrote no standards"
echo "PASS (2): an uninterrupted install still completes normally"

echo "ALL PASS: test_installer_signals.sh"
