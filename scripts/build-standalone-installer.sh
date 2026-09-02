#!/bin/sh
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
MANIFEST_FILE="$REPO_ROOT/asset-manifest.txt"
TEMPLATE_FILE="$SCRIPT_DIR/_standalone-installer-template.sh"
OUTPUT_FILE="$REPO_ROOT/standalone-installer.sh.txt"
TAG=""
STAGING_DIR=""

cleanup() {
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf "$STAGING_DIR"
  fi
}

fatal() {
  echo "ERROR: $*" >&2
  cleanup
  exit 1
}

usage() {
  cat <<USAGE
Usage: $0 --tag <release-tag> [--output <path>]

Options:
  --tag <release-tag>  Release tag to stamp into standalone-installer.sh
  --output <path>      Output path (default: ./standalone-installer.sh)
USAGE
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

manifest_source_path() {
  asset_class="$1"
  rel_path="$2"
  case "$asset_class" in
    prompt)
      printf '%s/prompts/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    standard)
      printf '%s/standards/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    template)
      if [ "$rel_path" = "builder-handoff-template.prompt.md" ]; then
        printf '%s/prompts/%s\n' "$REPO_ROOT" "$rel_path"
      else
        printf '%s/templates/%s\n' "$REPO_ROOT" "$rel_path"
      fi
      ;;
    schema)
      printf '%s/schemas/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    script)
      printf '%s/scripts/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    skill)
      printf '%s/skills/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    test)
      printf '%s/tests/%s\n' "$REPO_ROOT" "$rel_path"
      ;;
    *)
      fatal "unsupported asset class in manifest: $asset_class"
      ;;
  esac
}

stage_manifest_asset() {
  asset_class="$1"
  rel_path="$2"
  src_path=$(manifest_source_path "$asset_class" "$rel_path")
  dest_dir="$STAGING_DIR/src/$(dirname -- "$src_path" | sed "s|^$REPO_ROOT/||")"
  [ -f "$src_path" ] || fatal "manifest entry not found: $asset_class $rel_path"
  mkdir -p "$dest_dir"
  cp "$src_path" "$dest_dir/$(basename -- "$src_path")"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || fatal "--tag requires a value"
      TAG="$2"
      shift 2
      ;;
    --output)
      [ "$#" -ge 2 ] || fatal "--output requires a path"
      OUTPUT_FILE="$2"
      shift 2
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

[ -n "$TAG" ] || fatal "--tag is required"
[ -f "$MANIFEST_FILE" ] || fatal "asset-manifest.txt not found: $MANIFEST_FILE"
[ -f "$TEMPLATE_FILE" ] || fatal "template not found: $TEMPLATE_FILE"

echo "Running pre-publish scrub gate..."
PYTHONPATH="$REPO_ROOT/scripts" python3 "$REPO_ROOT/scripts/pre-publish-scan.py" --root "$REPO_ROOT" || fatal "pre-publish scrub gate failed - refusing to build the standalone installer"

STAGING_DIR=$(mktemp -d "${TMPDIR:-/tmp}/isanna-builder-standalone-build.XXXXXX")
trap 'cleanup' EXIT INT TERM
mkdir -p "$STAGING_DIR/src"

while IFS= read -r manifest_line || [ -n "$manifest_line" ]; do
  [ -n "$manifest_line" ] || continue
  asset_class=${manifest_line%% *}
  rel_path=${manifest_line#* }
  stage_manifest_asset "$asset_class" "$rel_path"
done < "$MANIFEST_FILE"

cp "$MANIFEST_FILE" "$STAGING_DIR/src/asset-manifest.txt"
cp "$REPO_ROOT/install.sh" "$STAGING_DIR/src/install.sh"

PAYLOAD_TGZ="$STAGING_DIR/payload.tar.gz"
python3 - "$STAGING_DIR/src" "$PAYLOAD_TGZ" <<'PY'
import gzip
import os
from pathlib import Path
import stat
import sys
import tarfile

src_root = Path(sys.argv[1])
output = sys.argv[2]
fixed_mtime = 1767225600  # 2026-01-01 00:00:00 UTC

with open(output, "wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for path in sorted(src_root.rglob("*"), key=lambda item: str(item.relative_to(src_root))):
                rel = str(path.relative_to(src_root))
                info = tar.gettarinfo(str(path), arcname=rel)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = fixed_mtime
                if path.is_dir():
                    info.mode = 0o755
                    tar.addfile(info)
                elif path.is_file():
                    executable = bool(info.mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                    info.mode = 0o755 if executable else 0o644
                    with open(path, "rb") as handle:
                        tar.addfile(info, handle)
PY

PAYLOAD_SHA256=$(sha256_file "$PAYLOAD_TGZ")
PAYLOAD_SIZE=$(wc -c < "$PAYLOAD_TGZ" | tr -d ' ')
MANIFEST_COUNT=$(grep -Ec '^(prompt|standard|template|schema|script|skill|test) ' "$MANIFEST_FILE")
PAYLOAD_B64="$STAGING_DIR/payload.b64"
python3 - "$PAYLOAD_TGZ" "$PAYLOAD_B64" <<'PY'
import base64
from pathlib import Path
import sys

payload = Path(sys.argv[1]).read_bytes()
encoded = base64.b64encode(payload).decode("ascii")
with open(sys.argv[2], "w", encoding="ascii", newline="\n") as handle:
    for index in range(0, len(encoded), 76):
        handle.write(encoded[index:index + 76])
        handle.write("\n")
PY

mkdir -p "$(dirname -- "$OUTPUT_FILE")"
awk \
  -v version="$TAG" \
  -v payload_sha256="$PAYLOAD_SHA256" \
  -v manifest_count="$MANIFEST_COUNT" \
  -v payload_file="$PAYLOAD_B64" '
  {
    gsub(/@@VERSION@@/, version)
    gsub(/@@PAYLOAD_SHA256@@/, payload_sha256)
    gsub(/@@MANIFEST_COUNT@@/, manifest_count)
    if ($0 == "__BUILDER_PAYLOAD__") {
      while ((getline line < payload_file) > 0) {
        print line
      }
      close(payload_file)
      print "__BUILDER_PAYLOAD__"
      next
    }
    print
  }
' "$TEMPLATE_FILE" > "$OUTPUT_FILE"

echo "Built standalone-installer.sh"
echo "  release    : $TAG"
echo "  payload    : $PAYLOAD_SIZE"
echo "  sha256     : $PAYLOAD_SHA256"
echo "  manifest   : $MANIFEST_COUNT entries"

VERSION_FILE="$(dirname -- "$OUTPUT_FILE")/standalone-installer.version.txt"
printf '%s\n' "$TAG" > "$VERSION_FILE"
echo "  version    : $VERSION_FILE"
