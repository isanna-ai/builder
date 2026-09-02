#!/usr/bin/env bash
# Build a fresh, local-only public history from precisely the scanner's publishable set.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET="/tmp/builder-public-dryrun"
DRY_RUN=1
SELF_TEST=0

usage() {
  echo "Usage: $0 [target-dir] [--dry-run] [--self-test]"
  echo "Creates a local one-commit export only; --dry-run is the default and only mode."
}

self_test() {
  # Prove the authoritative source gate cannot be bypassed by excluding the
  # maintainer denylist from the resulting public tree. Read a value rather
  # than embedding one of those private literals in this publishable script.
  local fixture output planted rel source
  fixture=$(mktemp -d /tmp/builder-export-self-test.XXXXXX)
  trap 'rm -rf "$fixture"' RETURN

  while IFS= read -r rel; do
    source="$ROOT/$rel"
    mkdir -p "$fixture/source/$(dirname -- "$rel")"
    cp -p "$source" "$fixture/source/$rel"
  done < <(cd "$ROOT" && git ls-files)
  git -C "$fixture/source" init -q
  git -C "$fixture/source" add --all

  if [ ! -f "$fixture/source/scripts/_scrub_private.txt" ]; then
    echo "--self-test needs scripts/_scrub_private.txt, the maintainer-only denylist. It is"
    echo "excluded from the public export by design, so this flag only works in the source tree."
    return 0
  fi
  planted=$(awk '!/^[[:space:]]*(#|$)/ { print; exit }' "$fixture/source/scripts/_scrub_private.txt")
  [ -n "$planted" ] || { echo "ERROR: export self-test has no denylist value to plant" >&2; return 1; }
  printf '\nexport self-test planted value: %s\n' "$planted" >> "$fixture/source/README.md"
  git -C "$fixture/source" add README.md

  output="$fixture/output.txt"
  if "$fixture/source/scripts/export-public.sh" "$fixture/export" >"$output" 2>&1; then
    echo "ERROR: export self-test failed: planted denylist value was exported" >&2
    return 1
  fi
  if ! grep -Fq "ERROR: source scrub failed; public export aborted" "$output"; then
    echo "ERROR: export self-test failed: source-gate abort was not clear" >&2
    sed -n '1,160p' "$output" >&2
    return 1
  fi
  echo "Export self-test PASS — planted denylist value aborted before export."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --self-test) SELF_TEST=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "ERROR: unsupported option: $1" >&2; exit 2 ;;
    *) TARGET="$1" ;;
  esac
  shift
done

if [ "$SELF_TEST" -eq 1 ]; then
  self_test
  exit $?
fi

case "$TARGET" in
  /tmp/*) ;;
  *) echo "ERROR: export target must be under /tmp: $TARGET" >&2; exit 2 ;;
esac
[ ! -e "$TARGET" ] || { echo "ERROR: target must not already exist: $TARGET" >&2; exit 2; }

# The private denylist intentionally does not enter the public tree, so an
# in-export-only scrub cannot detect one of its values. Refuse to construct an
# export unless the source set passes with the real denylist still available.
if ! (cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 make scrub); then
  echo "ERROR: source scrub failed; public export aborted" >&2
  exit 1
fi

mapfile -t PUBLISHABLE < <(cd "$ROOT" && PYTHONPATH=scripts python3 scripts/pre-publish-scan.py --list-publishable)
[ "${#PUBLISHABLE[@]}" -gt 0 ] || { echo "ERROR: publishable set is empty" >&2; exit 1; }
mkdir -p "$TARGET"

for rel in "${PUBLISHABLE[@]}"; do
  source="$ROOT/$rel"
  [ -f "$source" ] || { echo "ERROR: publishable path is not a regular file: $rel" >&2; exit 1; }
  mkdir -p "$TARGET/$(dirname -- "$rel")"
  cp -p "$source" "$TARGET/$rel"
done

# Keep this list explicit: an export must fail closed if the scanner's excludes
# change but this final structural assertion has not been reviewed as well.
for excluded in .builder .tg-bridge mission_control docs/PUBLISH.md \
  scripts/_scrub_private.txt docs/planning .mission-control .hive-claude .claude memory e2e; do
  [ ! -e "$TARGET/$excluded" ] || { echo "ERROR: excluded surface copied: $excluded" >&2; exit 1; }
done

git -C "$TARGET" init -q
git -C "$TARGET" add --all
(cd "$TARGET" && PYTHONDONTWRITEBYTECODE=1 make scrub)
EXPORT_IDENTITY_EMAIL='public-export@users.noreply.github.com' # publish-ok: neutral export identity
GIT_AUTHOR_NAME='Isanna Builder Export' \
GIT_AUTHOR_EMAIL="$EXPORT_IDENTITY_EMAIL" \
GIT_COMMITTER_NAME='Isanna Builder Export' \
GIT_COMMITTER_EMAIL="$EXPORT_IDENTITY_EMAIL" \
  git -C "$TARGET" commit -q -m 'Initial public export'

[ "$(git -C "$TARGET" rev-list --count HEAD)" = "1" ] || { echo "ERROR: export is not a single-root history" >&2; exit 1; }
[ -z "$(git -C "$TARGET" remote)" ] || { echo "ERROR: export unexpectedly has a git remote" >&2; exit 1; }
echo "Dry-run public export created: $TARGET (${#PUBLISHABLE[@]} files, one root commit)"
