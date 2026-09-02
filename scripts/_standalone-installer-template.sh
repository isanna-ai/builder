#!/bin/sh
set -e

TARGET="."
DRY_RUN=0
ASSUME_YES=0
AI_TARGET="copilot"
CODEX_HOME_ARG=""
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGING_DIR=""
TEMP_WORKSPACE_FILE=""
PAYLOAD_SHA256="@@PAYLOAD_SHA256@@"
PAYLOAD_MANIFEST_COUNT="@@MANIFEST_COUNT@@"
PAYLOAD_VERSION="@@VERSION@@"
UPDATE_URL_DEFAULT="https://raw.githubusercontent.com/isanna-ai/builder/main/standalone-installer.version.txt"
DOWNLOAD_URL_DEFAULT="https://raw.githubusercontent.com/isanna-ai/builder/main/standalone-installer.sh.txt"

check_for_update() {
  if [ "${BUILDER_SKIP_UPDATE_CHECK:-0}" = "1" ]; then
    return 0
  fi
  url="${BUILDER_UPDATE_URL:-$UPDATE_URL_DEFAULT}"
  download_url="${BUILDER_DOWNLOAD_URL:-$DOWNLOAD_URL_DEFAULT}"
  remote_version=""
  fetcher=""
  if command -v curl >/dev/null 2>&1; then
    fetcher="curl"
    remote_version=$(curl -fsSL --connect-timeout 3 --max-time 6 "$url" 2>/dev/null | head -n1 | tr -d ' \t\r\n')
  elif command -v wget >/dev/null 2>&1; then
    fetcher="wget"
    remote_version=$(wget -qO- --timeout=6 --tries=1 "$url" 2>/dev/null | head -n1 | tr -d ' \t\r\n')
  else
    echo "NOTE: update check skipped (no curl or wget); continuing with embedded $PAYLOAD_VERSION." >&2
    return 0
  fi
  if [ -z "$remote_version" ]; then
    echo "NOTE: update check failed via $fetcher (network/proxy blocked or URL unreachable); continuing with embedded $PAYLOAD_VERSION." >&2
    return 0
  fi
  if [ "$remote_version" = "$PAYLOAD_VERSION" ]; then
    echo "Update check : up to date ($PAYLOAD_VERSION)"
    return 0
  fi
  echo "" >&2
  echo "WARNING: a newer standalone installer version is available." >&2
  echo "  embedded version : $PAYLOAD_VERSION" >&2
  echo "  latest in repo   : $remote_version" >&2
  echo "  download URL     : $download_url" >&2
  echo "" >&2
  if [ "$ASSUME_YES" = "1" ]; then
    echo "Continuing with embedded $PAYLOAD_VERSION (--yes)." >&2
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "No interactive terminal; aborting. Re-run with --yes to proceed with the embedded version anyway." >&2
    cleanup
    exit 0
  fi
  printf "Proceed with embedded %s anyway? [y/N] " "$PAYLOAD_VERSION" >&2
  IFS= read -r answer || answer=""
  case "$answer" in
    y|Y|yes|YES) echo "Continuing with embedded $PAYLOAD_VERSION." >&2 ;;
    *) echo "Aborted by user. Download the newer installer from $download_url and re-run." >&2; cleanup; exit 0 ;;
  esac
}

cleanup() {
  if [ -n "$TEMP_WORKSPACE_FILE" ] && [ -f "$TEMP_WORKSPACE_FILE" ]; then
    rm -f "$TEMP_WORKSPACE_FILE"
  fi
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf "$STAGING_DIR"
  fi
}

# `trap 'cleanup' EXIT INT TERM` -- what this script used to do -- points the signal traps at a
# handler that only deletes the staging directory and never exits. A trap handler that does not
# exit RESUMES the script, so the signal was SWALLOWED: measured on the shipped v0.3.1 artifact,
# `kill -INT`/`kill -TERM` to this shell gave rc=0 with all 109 files written and the success
# banner printed, 6 runs out of 6. Cancelling did nothing at all.
#
# install.sh carries the same fix for the same reason; this is its twin and must not drift from
# it. Clean up, restore the default disposition, and re-raise, so the caller sees a real signal
# death (128+n) instead of a success it did not get.
on_signal() {
  cleanup
  trap - "$1"
  kill -s "$1" $$
}

fatal() {
  echo "ERROR: $*" >&2
  cleanup
  exit 1
}

usage() {
  cat <<USAGE
Usage: $0 [--target <path>] [--ai copilot|claude|codex] [--codex-home <path>] [--dry-run] [--yes]

Options:
  --target <path>       Install into a specific repo or workspace root
  --ai <tool>           Target AI tool: copilot (default), claude, or codex
  --codex-home <path>   Codex home for --ai codex (default: CODEX_HOME or ~/.codex)
  --dry-run             Print planned actions without writing files
  --yes                 Skip confirmation prompt
USAGE
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fatal "required tool not found: $1"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
    return
  fi
  fatal "required tool not found: sha256sum"
}

manifest_count() {
  if [ ! -f "$1" ]; then
    fatal "embedded manifest missing: $1"
  fi
  grep -Ec '^(prompt|standard|template|schema|script|skill|test) ' "$1"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      [ "$#" -ge 2 ] || fatal "--target requires a path"
      TARGET="$2"
      shift 2
      ;;
    --ai)
      [ "$#" -ge 2 ] || fatal "--ai requires a value (copilot, claude, or codex)"
      AI_TARGET="$2"
      case "$AI_TARGET" in
        copilot|claude|codex) ;;
        *) fatal "--ai must be 'copilot', 'claude', or 'codex'" ;;
      esac
      shift 2
      ;;
    --codex-home)
      [ "$#" -ge 2 ] || fatal "--codex-home requires a path"
      CODEX_HOME_ARG="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --builder-ref)
      [ "$#" -ge 2 ] || fatal "--builder-ref requires a value"
      fatal "--builder-ref is not supported by standalone-installer.sh (this build is pinned to $PAYLOAD_VERSION)"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fatal "Unknown argument: $1"
      ;;
  esac
done

require_tool sh
require_tool base64
require_tool gzip
require_tool tar
require_tool mkdir
require_tool mv
require_tool rm
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  fatal "required tool not found: sha256sum"
fi

check_for_update

STAGING_DIR="${TMPDIR:-/tmp}/builder-standalone-$$"
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP
mkdir -p "$STAGING_DIR/src"

cat > "$STAGING_DIR/payload.b64" <<'__BUILDER_PAYLOAD__'
__BUILDER_PAYLOAD__

decode_flag="-d"
if ! base64 "$decode_flag" < "$STAGING_DIR/payload.b64" > "$STAGING_DIR/payload.tar.gz" 2>/dev/null; then
  fatal "failed to decode embedded payload"
fi

actual_sha256=$(sha256_file "$STAGING_DIR/payload.tar.gz")
if [ "$actual_sha256" != "$PAYLOAD_SHA256" ]; then
  fatal "payload integrity mismatch (expected $PAYLOAD_SHA256, got $actual_sha256)"
fi

tar -xzf "$STAGING_DIR/payload.tar.gz" -C "$STAGING_DIR/src"

actual_manifest_count=$(manifest_count "$STAGING_DIR/src/asset-manifest.txt")
if [ "$actual_manifest_count" != "$PAYLOAD_MANIFEST_COUNT" ]; then
  fatal "embedded manifest declares $PAYLOAD_MANIFEST_COUNT assets but extracted manifest declares $actual_manifest_count"
fi

TARGET_ABS=$(CDPATH= cd -- "$TARGET" 2>/dev/null && pwd) || fatal "Target path does not exist: $TARGET"
HAS_GIT=0
[ -d "$TARGET_ABS/.git" ] && HAS_GIT=1
HAS_WORKSPACE=0
for workspace_candidate in "$TARGET_ABS"/*.code-workspace; do
  [ -e "$workspace_candidate" ] || continue
  HAS_WORKSPACE=1
  break
done

# The SAME guard install.sh applies, applied here for the same reason. This used to fabricate a
# throwaway `.code-workspace` marker so the guard downstream would pass, which meant the
# standalone path -- the one the README recommends to proxy-blocked users -- would happily write
# `.builder/` and `.github/` into a home directory that install.sh refuses outright. A safety
# property that holds on one documented path and not the other is not a safety property.
if [ "$HAS_GIT" -ne 1 ] && [ "$HAS_WORKSPACE" -ne 1 ]; then
  fatal "Unsupported target: must contain .git/ or a .code-workspace file: $TARGET_ABS"
fi

set +e
if [ -n "$CODEX_HOME_ARG" ]; then
  BUILDER_INSTALL_PROVENANCE=standalone BUILDER_REF="$PAYLOAD_VERSION" \
    /bin/sh "$STAGING_DIR/src/install.sh" \
    --target "$TARGET_ABS" \
    --ai "$AI_TARGET" \
    --codex-home "$CODEX_HOME_ARG" \
    $(if [ "$DRY_RUN" = "1" ]; then printf '%s ' '--dry-run'; fi)\
    $(if [ "$ASSUME_YES" = "1" ]; then printf '%s ' '--yes'; fi)
  install_status=$?
else
  BUILDER_INSTALL_PROVENANCE=standalone BUILDER_REF="$PAYLOAD_VERSION" \
    /bin/sh "$STAGING_DIR/src/install.sh" \
    --target "$TARGET_ABS" \
    --ai "$AI_TARGET" \
    $(if [ "$DRY_RUN" = "1" ]; then printf '%s ' '--dry-run'; fi)\
    $(if [ "$ASSUME_YES" = "1" ]; then printf '%s ' '--yes'; fi)
  install_status=$?
fi
set -e

if [ "$install_status" -ne 0 ]; then
  exit "$install_status"
fi

echo "Provenance : standalone (release $PAYLOAD_VERSION)"
