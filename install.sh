#!/bin/sh
set -e

TARGET="."
DRY_RUN=0
ASSUME_YES=0
SPEC_REF="main"
AI_TARGET="copilot"
CODEX_HOME_ARG=""

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGING_DIR=""
CREATED=""
UPDATED=""
PRESERVED=""
REMOVED=""

cleanup() {
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf "$STAGING_DIR"
  fi
}

# A trap handler that does not exit RESUMES the script. Trapping INT/TERM/HUP straight onto
# `cleanup` -- which only removes the staging dir -- therefore SWALLOWED the signal: Ctrl-C during
# a `curl | bash --yes` install ran to completion and printed the success banner (measured: rc=0,
# all files written, banner shown). Worse under TERM, where cleanup raced the copy loop and deleted
# the staging dir mid-copy, leaving a partial .builder/ behind.
#
# So: clean up, restore the default disposition, and re-raise, which is the only way the caller
# sees a real signal death (128+n) rather than a success.
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

append_line() {
  current="$1"
  line="$2"
  if [ -z "$current" ]; then
    printf '%s' "$line"
  else
    printf '%s\n%s' "$current" "$line"
  fi
}

print_lines() {
  text="$1"
  if [ -n "$text" ]; then
    printf '%s\n' "$text"
  fi
}

usage() {
  cat <<USAGE
Usage: $0 [--target <path>] [--ai copilot|claude|codex] [--codex-home <path>] [--dry-run] [--yes] [--builder-ref <ref>]

Options:
  --target <path>       Install into a specific repo or workspace root
  --ai <tool>           Target AI tool: copilot (default), claude, or codex
  --codex-home <path>   Codex home for --ai codex (default: CODEX_HOME or ~/.codex)
  --dry-run             Print planned actions without writing files
  --yes                 Skip confirmation prompt
  --builder-ref <ref>   Fetch a specific branch, tag, or commit

Examples:
  $0 --yes                               # Copilot, current repo
  $0 --ai claude --target /path/to/repo  # Claude Code, a specific repo
  $0 --ai codex --yes                    # Codex global skill
  $0 --dry-run                           # preview the file plan only
  $0 --builder-ref vX.Y.Z --yes          # pin to a release tag

Docs & troubleshooting: https://github.com/isanna-ai/builder#troubleshooting
USAGE
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
      SPEC_REF="$2"
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

# Prompt directory depends on target AI tool
if [ "$AI_TARGET" = "codex" ]; then
  PROMPT_DIR=""
  PROMPT_EXT=".prompt.md"
elif [ "$AI_TARGET" = "claude" ]; then
  PROMPT_DIR=".claude/commands"
  PROMPT_EXT=".md"
else
  PROMPT_DIR=".github/prompts"
  PROMPT_EXT=".prompt.md"
fi

# DETECT
TARGET_ABS=$(CDPATH= cd -- "$TARGET" 2>/dev/null && pwd) || fatal "Target path does not exist: $TARGET"
WORKSPACE_FILE=$(find "$TARGET_ABS" -maxdepth 1 -name '*.code-workspace' | head -n 1)
HAS_WORKSPACE=0
[ -n "$WORKSPACE_FILE" ] && HAS_WORKSPACE=1
HAS_GIT=0
# -e, not -d: in a git WORKTREE or SUBMODULE, `.git` is a regular FILE containing a gitdir
# pointer. Testing for a directory refused those targets with "must contain .git/" while the
# user could plainly see a .git right there.
[ -e "$TARGET_ABS/.git" ] && HAS_GIT=1
HAS_BUILDER=0
[ -d "$TARGET_ABS/.builder" ] && HAS_BUILDER=1
# Detect legacy .oak directory for migration
HAS_LEGACY_OAK=0
[ -d "$TARGET_ABS/.oak" ] && HAS_LEGACY_OAK=1
HAS_ISANNA_PROMPTS=0
if [ -n "$PROMPT_DIR" ] && [ -d "$TARGET_ABS/$PROMPT_DIR" ] && ls "$TARGET_ABS/$PROMPT_DIR"/isanna-*${PROMPT_EXT} >/dev/null 2>&1; then
  HAS_ISANNA_PROMPTS=1
fi
CODEX_HOME_ABS=""
CODEX_SKILL_DIR=""
if [ "$AI_TARGET" = "codex" ]; then
  CODEX_HOME_BASE=${CODEX_HOME_ARG:-${CODEX_HOME:-$HOME/.codex}}
  case "$CODEX_HOME_BASE" in
    ~/*) CODEX_HOME_BASE="$HOME/${CODEX_HOME_BASE#~/}" ;;
  esac
  case "$CODEX_HOME_BASE" in
    /*) CODEX_HOME_ABS="$CODEX_HOME_BASE" ;;
    *) CODEX_HOME_ABS="$PWD/$CODEX_HOME_BASE" ;;
  esac
  CODEX_SKILL_DIR="$CODEX_HOME_ABS/skills/builder"
fi

if [ "$HAS_GIT" -ne 1 ] && [ "$HAS_WORKSPACE" -ne 1 ]; then
  fatal "Unsupported target: must contain .git/ or a .code-workspace file"
fi

# REPORT
if [ "$HAS_WORKSPACE" -eq 1 ]; then
  TARGET_KIND="multi-root workspace"
else
  TARGET_KIND="repository"
fi

echo "[DETECT] Target: $TARGET_ABS"
echo "[DETECT] Type: $TARGET_KIND"
echo "[DETECT] AI tool: $AI_TARGET"
echo "[DETECT] Existing .builder/: $HAS_BUILDER"
echo "[DETECT] Existing isanna prompts: $HAS_ISANNA_PROMPTS"
if [ "$AI_TARGET" = "codex" ]; then
  echo "[DETECT] Codex skill dir: $CODEX_SKILL_DIR"
fi
if [ "$HAS_LEGACY_OAK" -eq 1 ]; then
  echo "[DETECT] Legacy .oak/ found — will migrate to .builder/"
fi

# PLAN
PLAN_LINES=""
plan_add() {
  path="$1"
  if [ -e "$path" ]; then
    PLAN_LINES=$(append_line "$PLAN_LINES" "[UPDATE] $path")
  else
    PLAN_LINES=$(append_line "$PLAN_LINES" "[CREATE] $path")
  fi
}

if [ -n "$PROMPT_DIR" ]; then
  plan_add "$TARGET_ABS/$PROMPT_DIR"
fi
if [ "$AI_TARGET" = "codex" ]; then
  plan_add "$CODEX_SKILL_DIR"
fi
plan_add "$TARGET_ABS/.builder"
plan_add "$TARGET_ABS/.builder/templates"
plan_add "$TARGET_ABS/.builder/schemas"
plan_add "$TARGET_ABS/.builder/scripts/_validators"
plan_add "$TARGET_ABS/.builder/scripts/_telemetry"
plan_add "$TARGET_ABS/.builder/builder-standards.md"
plan_add "$TARGET_ABS/.builder/builder-tdd.md"
plan_add "$TARGET_ABS/.builder/templates/spec.yaml"
plan_add "$TARGET_ABS/.builder/templates/intent.yaml"
plan_add "$TARGET_ABS/.builder/templates/intent-object.yaml"
plan_add "$TARGET_ABS/.builder/templates/requirements.yaml"
plan_add "$TARGET_ABS/.builder/templates/design.yaml"
plan_add "$TARGET_ABS/.builder/templates/gate-lane-policy.yaml"
plan_add "$TARGET_ABS/.builder/templates/tasks.yaml"
plan_add "$TARGET_ABS/.builder/templates/handoff.yaml"
plan_add "$TARGET_ABS/.builder/templates/setup-decisions.yaml"
# Planning skill — lane-neutral copy referenced by prompt load_sets
plan_add "$TARGET_ABS/.builder/skills/planning/SKILL.md"
# isanna-builder agent skills — shipped behavioral set, installed to the native skills dir
for bn in isanna-builder isanna-builder-authoring isanna-builder-dispatcher isanna-builder-recorder isanna-builder-reviews isanna-builder-roadmap isanna-builder-ssot; do
  case "$AI_TARGET" in
    claude)  plan_add "$TARGET_ABS/.claude/skills/$bn/SKILL.md" ;;
    copilot) plan_add "$TARGET_ABS/.github/skills/$bn/SKILL.md" ;;
    codex)   plan_add "$CODEX_HOME_ABS/skills/$bn/SKILL.md" ;;
  esac
done
if [ -e "$TARGET_ABS/.builder/constitution.md" ] || [ -e "$TARGET_ABS/.oak/constitution.md" ]; then
  PLAN_LINES=$(append_line "$PLAN_LINES" "[PRESERVE] $TARGET_ABS/.builder/constitution.md")
else
  PLAN_LINES=$(append_line "$PLAN_LINES" "[CREATE] $TARGET_ABS/.builder/constitution.md")
fi
if [ "$AI_TARGET" = "copilot" ]; then
  if [ -e "$TARGET_ABS/.github/skills/planning/SKILL.md" ]; then
    PLAN_LINES=$(append_line "$PLAN_LINES" "[PRESERVE] $TARGET_ABS/.github/skills/planning/SKILL.md")
  else
    PLAN_LINES=$(append_line "$PLAN_LINES" "[CREATE] $TARGET_ABS/.github/skills/planning/SKILL.md")
  fi
fi
if [ -e "$TARGET_ABS/$PROMPT_DIR/isanna-journey${PROMPT_EXT}" ]; then
  PLAN_LINES=$(append_line "$PLAN_LINES" "[DELETE] $TARGET_ABS/$PROMPT_DIR/isanna-journey${PROMPT_EXT}")
fi
if [ "$HAS_LEGACY_OAK" -eq 1 ]; then
  PLAN_LINES=$(append_line "$PLAN_LINES" "[MIGRATE] $TARGET_ABS/.oak/ → $TARGET_ABS/.builder/")
fi

if [ -n "$PROMPT_DIR" ]; then
  for prompt in "$SCRIPT_DIR"/prompts/isanna-*.prompt.md; do
    [ -f "$prompt" ] || continue
    base=$(basename "$prompt")
    if [ "$AI_TARGET" = "claude" ]; then
      # Convert isanna-foo.prompt.md → isanna-foo.md for Claude commands
      base=$(echo "$base" | sed 's/\.prompt\.md$/.md/')
    fi
    plan_add "$TARGET_ABS/$PROMPT_DIR/$base"
  done
  if [ -f "$SCRIPT_DIR/prompts/builder-handoff-template.prompt.md" ]; then
    if [ "$AI_TARGET" = "claude" ]; then
      plan_add "$TARGET_ABS/$PROMPT_DIR/builder-handoff-template.md"
    else
      plan_add "$TARGET_ABS/$PROMPT_DIR/builder-handoff-template.prompt.md"
    fi
  fi
fi

echo "[PLAN] Proposed file actions:"
print_lines "$PLAN_LINES"

# CONFIRM
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[CONFIRM] Dry run enabled. No files were written."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  # Read the confirmation from the TERMINAL, not stdin: the documented install is
  # `curl -fsSL ... | sh`, where stdin is the pipe carrying this script. That is why this
  # is `/dev/tty` and not a plain `read`.
  #
  # But `/dev/tty` only exists when there IS a controlling terminal. In CI, a container
  # started without a TTY, a systemd unit or under nohup, opening it fails -- and this used
  # to fail RAW, after printing the whole file plan:
  #     Proceed with install? [y/N] install.sh: 288: cannot open /dev/tty: No such device or address
  # rc=2, no install, and nothing telling the user that `--yes` is the answer.
  #
  # Note the guard is NOT `[ ! -t 0 ]`, which the standalone installer can afford because it
  # is run as a file. Here stdin is a pipe on the primary documented path, so testing stdin
  # would refuse the exact install everyone runs. Test what we actually need: can we open the
  # terminal?
  # The probe runs in a SUBSHELL on purpose. `exec` is a special builtin, so a failed
  # redirection on it is fatal to the shell itself in dash -- probing inline would produce
  # the very rc=2 death this guard exists to prevent. The subshell absorbs that.
  if ! ( exec </dev/tty ) 2>/dev/null; then
    echo "" >&2
    echo "No controlling terminal, so there is nothing to read a confirmation from." >&2
    echo "Re-run with --yes to install without the prompt." >&2
    cleanup
    exit 1
  fi
  printf 'Proceed with install? [y/N] '
  read ans </dev/tty
  case "$ans" in
    y|Y|yes|YES) ;;
    *)
      echo "Cancelled."
      exit 0
      ;;
  esac
fi

# STAGE
STAGING_DIR="$TARGET_ABS/.builder-staging-$$"
# The staging dir lives INSIDE the user's repo, so anything that exits without cleanup leaves
# visible debris in their working tree (and `git status` shows it -- our own .gitignore rule for
# it does not exist in their repo). cleanup() was only reachable via fatal(), so a `set -e`
# abort or a signal skipped it.
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP
mkdir -p "$STAGING_DIR/prompts" "$STAGING_DIR/standards" "$STAGING_DIR/templates" "$STAGING_DIR/skills/planning" "$STAGING_DIR/skills/builder/agents" "$STAGING_DIR/scripts/_validators" "$STAGING_DIR/scripts/_telemetry" "$STAGING_DIR/scripts/_constitution" "$STAGING_DIR/scripts/_dispatch_runtime" "$STAGING_DIR/scripts/_sync" "$STAGING_DIR/schemas" "$STAGING_DIR/tests"

copy_local_assets() {
  [ -f "$SCRIPT_DIR/asset-manifest.txt" ] || return 1
  cp "$SCRIPT_DIR/asset-manifest.txt" "$STAGING_DIR/asset-manifest.txt"
  stage_manifest_assets "$SCRIPT_DIR"
}

stage_manifest_assets() {
  source_root="$1"
  # The manifest is the release's single source of truth.  Keeping this loop
  # generic means a new asset cannot silently be omitted from either path.
  while IFS=' ' read -r asset_class rel_path || [ -n "$asset_class" ]; do
    [ -n "$asset_class" ] || continue
    case "$asset_class" in
      prompt) source="prompts/$rel_path"; dest="prompts/$rel_path" ;;
      standard) source="standards/$rel_path"; dest="standards/$rel_path" ;;
      template)
        if [ "$rel_path" = "builder-handoff-template.prompt.md" ]; then
          source="prompts/$rel_path"; dest="prompts/$rel_path"
        else
          source="templates/$rel_path"; dest="templates/$rel_path"
        fi
        ;;
      schema) source="schemas/$rel_path"; dest="schemas/$rel_path" ;;
      script) source="scripts/$rel_path"; dest="scripts/$rel_path" ;;
      skill) source="skills/$rel_path"; dest="skills/$rel_path" ;;
      test) source="tests/$rel_path"; dest="tests/$rel_path" ;;
      *) fatal "Unknown asset class in remote manifest: $asset_class" ;;
    esac
    mkdir -p "$STAGING_DIR/$(dirname -- "$dest")"
    if [ "$source_root" = "remote" ]; then
      curl -fsSL "$RAW_BASE/$source" -o "$STAGING_DIR/$dest"
    else
      cp "$source_root/$source" "$STAGING_DIR/$dest" || fatal "Manifest asset missing: $asset_class $rel_path"
    fi
  done < "$STAGING_DIR/asset-manifest.txt"
}

has_compatible_python3() {
  command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

fetch_remote_assets() {
  RAW_BASE="https://raw.githubusercontent.com/isanna-ai/builder/$SPEC_REF"
  curl -fsSL "$RAW_BASE/asset-manifest.txt" -o "$STAGING_DIR/asset-manifest.txt"
  stage_manifest_assets remote

  # Fail before touching the target if a remote bundle is internally incomplete.
  if has_compatible_python3; then
    PYTHONPATH="$STAGING_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import _validators' \
      || fatal "Remote validator bundle is not importable"
  else
    echo "[STAGE] Compatible python3 not found; skipping remote validator-import check"
  fi
}

echo "[STAGE] Staging assets in $STAGING_DIR"
if ! copy_local_assets; then
  echo "[STAGE] Local asset bundle not found, fetching from remote ref '$SPEC_REF'"
  fetch_remote_assets || fatal "Failed to fetch remote assets. Retry later or run from a local Builder checkout."
fi

PROMPT_COUNT=$(ls "$STAGING_DIR/prompts"/isanna-*.prompt.md 2>/dev/null | wc -l | tr -d ' ')
# Derive expected prompt count from asset-manifest (number of 'prompt' lines)
MANIFEST_PATH="$STAGING_DIR/asset-manifest.txt"
[ -f "$MANIFEST_PATH" ] || fatal "Staged asset manifest is missing"
EXPECTED_PROMPT_COUNT=$(grep -c '^prompt ' "$MANIFEST_PATH" 2>/dev/null || echo "0")
[ "$PROMPT_COUNT" = "$EXPECTED_PROMPT_COUNT" ] || fatal "Staged prompts are incomplete (manifest declares $EXPECTED_PROMPT_COUNT isanna-*.prompt.md files, got $PROMPT_COUNT)"

# Release-surface check: scan AGENTS.md and README.md in the source root for stale hardcoded counts.
# Any integer in an "N prompts" or "N isanna-*.prompt.md" context that does not match manifest count is a fault.
for release_doc in AGENTS.md README.md; do
  doc_path="$SCRIPT_DIR/$release_doc"
  [ -f "$doc_path" ] || continue
  # Extract integers immediately preceding "prompts" or "prompt.md" phrases
  stale=$(grep -oE '[0-9]+[[:space:]]+(prompts|isanna-\*\.prompt\.md|prompt\.md files)' "$doc_path" 2>/dev/null | grep -vE "^${EXPECTED_PROMPT_COUNT}[[:space:]]" || true)
  if [ -n "$stale" ]; then
    fatal "$release_doc references \"$stale\"; expected manifest count ($EXPECTED_PROMPT_COUNT prompts)"
  fi
done
if [ ! -f "$STAGING_DIR/scripts/validate-spec.py" ]; then
  echo "[STAGE] Optional validator scripts/validate-spec.py not staged — skipping (soft-dep)"
fi
[ -f "$STAGING_DIR/scripts/render-spec-artifacts.py" ] || fatal "Missing staged scripts/render-spec-artifacts.py"
[ -f "$STAGING_DIR/scripts/record-workflow-event.py" ] || fatal "Missing staged scripts/record-workflow-event.py"
[ -f "$STAGING_DIR/scripts/analyze-workflow-telemetry.py" ] || fatal "Missing staged scripts/analyze-workflow-telemetry.py"
[ -f "$STAGING_DIR/scripts/_validators/__init__.py" ] || fatal "Missing staged scripts/_validators/__init__.py"
[ -f "$STAGING_DIR/scripts/_telemetry/__init__.py" ] || fatal "Missing staged scripts/_telemetry/__init__.py"
[ -f "$STAGING_DIR/scripts/_constitution/__init__.py" ] || fatal "Missing staged scripts/_constitution/__init__.py"
[ -f "$STAGING_DIR/scripts/_dispatch_runtime/gate_evidence.py" ] || fatal "Missing staged scripts/_dispatch_runtime/gate_evidence.py"
[ -f "$STAGING_DIR/scripts/_sync/readmit.py" ] || fatal "Missing staged scripts/_sync/readmit.py"
[ -f "$STAGING_DIR/scripts/_sync/locking.py" ] || fatal "Missing staged scripts/_sync/locking.py"
[ -f "$STAGING_DIR/schemas/tasks.schema.yaml" ] || fatal "Missing staged schemas/tasks.schema.yaml"
[ -f "$STAGING_DIR/schemas/runner.schema.yaml" ] || fatal "Missing staged schemas/runner.schema.yaml"
[ -f "$STAGING_DIR/schemas/workflow-event.schema.yaml" ] || fatal "Missing staged schemas/workflow-event.schema.yaml"
[ -f "$STAGING_DIR/schemas/telemetry-report.schema.yaml" ] || fatal "Missing staged schemas/telemetry-report.schema.yaml"
[ -f "$STAGING_DIR/standards/builder-standards.md" ] || fatal "Missing staged standards/builder-standards.md"
[ -f "$STAGING_DIR/standards/builder-tdd.md" ] || fatal "Missing staged standards/builder-tdd.md"
[ -f "$STAGING_DIR/standards/builder-workflow.md" ] || fatal "Missing staged standards/builder-workflow.md"
[ -f "$STAGING_DIR/templates/spec.yaml" ] || fatal "Missing staged templates/spec.yaml"
[ -f "$STAGING_DIR/templates/intent.yaml" ] || fatal "Missing staged templates/intent.yaml"
[ -f "$STAGING_DIR/templates/intent-object.yaml" ] || fatal "Missing staged templates/intent-object.yaml"
[ -f "$STAGING_DIR/templates/requirements.yaml" ] || fatal "Missing staged templates/requirements.yaml"
[ -f "$STAGING_DIR/templates/design.yaml" ] || fatal "Missing staged templates/design.yaml"
[ -f "$STAGING_DIR/templates/gate-lane-policy.yaml" ] || fatal "Missing staged templates/gate-lane-policy.yaml"
[ -f "$STAGING_DIR/templates/tasks.yaml" ] || fatal "Missing staged templates/tasks.yaml"
[ -f "$STAGING_DIR/templates/handoff.yaml" ] || fatal "Missing staged templates/handoff.yaml"
[ -f "$STAGING_DIR/templates/setup-decisions.yaml" ] || fatal "Missing staged templates/setup-decisions.yaml"
[ -f "$STAGING_DIR/templates/constitution.md" ] || fatal "Missing staged templates/constitution.md"
[ -f "$STAGING_DIR/skills/planning/SKILL.md" ] || fatal "Missing staged skills/planning/SKILL.md"
[ -f "$STAGING_DIR/skills/isanna-builder/SKILL.md" ] || fatal "Missing staged skills/isanna-builder/SKILL.md (the shipped builder skill set)"
if [ "$AI_TARGET" = "codex" ]; then
  [ -f "$STAGING_DIR/skills/builder/SKILL.md" ] || fatal "Missing staged skills/builder/SKILL.md"
fi

# MIGRATE legacy .oak → .builder
if [ "$HAS_LEGACY_OAK" -eq 1 ] && [ ! -d "$TARGET_ABS/.builder" ]; then
  echo "[MIGRATE] Moving .oak/ → .builder/"
  mv "$TARGET_ABS/.oak" "$TARGET_ABS/.builder"
  # Rename legacy standard files if present
  [ -f "$TARGET_ABS/.builder/oak-standards.md" ] && mv "$TARGET_ABS/.builder/oak-standards.md" "$TARGET_ABS/.builder/builder-standards.md"
  [ -f "$TARGET_ABS/.builder/oak-tdd.md" ] && mv "$TARGET_ABS/.builder/oak-tdd.md" "$TARGET_ABS/.builder/builder-tdd.md"
fi

# INSTALL
if [ -n "$PROMPT_DIR" ]; then
  mkdir -p "$TARGET_ABS/$PROMPT_DIR"
fi
mkdir -p "$TARGET_ABS/.builder" "$TARGET_ABS/.builder/templates"

if [ -n "$PROMPT_DIR" ]; then
  for src in "$STAGING_DIR"/prompts/isanna-*.prompt.md; do
    base=$(basename "$src")
    if [ "$AI_TARGET" = "claude" ]; then
      base=$(echo "$base" | sed 's/\.prompt\.md$/.md/')
    fi
    dst="$TARGET_ABS/$PROMPT_DIR/$base"
    tmp="$dst.tmp.$$"
    cp "$src" "$tmp"
    mv "$tmp" "$dst"
    if [ -e "$dst" ]; then
      UPDATED=$(append_line "$UPDATED" "$dst")
    fi
  done

  # Remove stale Builder prompts that are no longer present in the staged
  # release bundle. This keeps upgrades aligned with the source package surface.
  for existing_prompt in "$TARGET_ABS/$PROMPT_DIR"/isanna-*${PROMPT_EXT}; do
    [ -f "$existing_prompt" ] || continue
    existing_base=$(basename "$existing_prompt")
    if [ "$AI_TARGET" = "claude" ]; then
      staged_base=$(echo "$existing_base" | sed 's/\.md$/.prompt.md/')
    else
      staged_base="$existing_base"
    fi
    if [ ! -f "$STAGING_DIR/prompts/$staged_base" ]; then
      rm -f "$existing_prompt"
      REMOVED=$(append_line "$REMOVED" "$existing_prompt")
    fi
  done

  # Remove prompts from the retired /sp-* command namespace during upgrades.
  for legacy_prompt in "$TARGET_ABS/$PROMPT_DIR"/sp-*${PROMPT_EXT}; do
    [ -f "$legacy_prompt" ] || continue
    rm -f "$legacy_prompt"
    REMOVED=$(append_line "$REMOVED" "$legacy_prompt")
  done

  if [ -f "$STAGING_DIR/prompts/builder-handoff-template.prompt.md" ]; then
    if [ "$AI_TARGET" = "claude" ]; then
      dst="$TARGET_ABS/$PROMPT_DIR/builder-handoff-template.md"
    else
      dst="$TARGET_ABS/$PROMPT_DIR/builder-handoff-template.prompt.md"
    fi
    tmp="$dst.tmp.$$"
    cp "$STAGING_DIR/prompts/builder-handoff-template.prompt.md" "$tmp"
    mv "$tmp" "$dst"
    UPDATED=$(append_line "$UPDATED" "$dst")
  fi

  # Remove stale journey prompt
  if [ -e "$TARGET_ABS/$PROMPT_DIR/isanna-journey${PROMPT_EXT}" ]; then
    rm -f "$TARGET_ABS/$PROMPT_DIR/isanna-journey${PROMPT_EXT}"
    REMOVED=$(append_line "$REMOVED" "$TARGET_ABS/$PROMPT_DIR/isanna-journey${PROMPT_EXT}")
  fi
fi
# Also clean legacy Copilot path if installing for Claude
if [ "$AI_TARGET" = "claude" ] && [ -e "$TARGET_ABS/.github/prompts/isanna-journey.prompt.md" ]; then
  rm -f "$TARGET_ABS/.github/prompts/isanna-journey.prompt.md"
  REMOVED=$(append_line "$REMOVED" "$TARGET_ABS/.github/prompts/isanna-journey.prompt.md (legacy)")
fi

if [ "$AI_TARGET" = "codex" ]; then
  if [ -d "$CODEX_SKILL_DIR" ]; then
    UPDATED=$(append_line "$UPDATED" "$CODEX_SKILL_DIR")
  else
    CREATED=$(append_line "$CREATED" "$CODEX_SKILL_DIR")
  fi
  mkdir -p "$CODEX_SKILL_DIR/agents" "$CODEX_SKILL_DIR/prompts" "$CODEX_SKILL_DIR/standards" "$CODEX_SKILL_DIR/references"
  cp "$STAGING_DIR/skills/builder/SKILL.md" "$CODEX_SKILL_DIR/SKILL.md"
  if [ -f "$STAGING_DIR/skills/builder/agents/openai.yaml" ]; then
    cp "$STAGING_DIR/skills/builder/agents/openai.yaml" "$CODEX_SKILL_DIR/agents/openai.yaml"
  fi
  rm -f "$CODEX_SKILL_DIR/prompts"/isanna-*.prompt.md "$CODEX_SKILL_DIR/prompts"/sp-*.prompt.md "$CODEX_SKILL_DIR/prompts/builder-handoff-template.prompt.md" 2>/dev/null || true
  cp "$STAGING_DIR"/prompts/isanna-*.prompt.md "$CODEX_SKILL_DIR/prompts/" 2>/dev/null || true
  [ -f "$STAGING_DIR/prompts/builder-handoff-template.prompt.md" ] && cp "$STAGING_DIR/prompts/builder-handoff-template.prompt.md" "$CODEX_SKILL_DIR/prompts/"
  rm -f "$CODEX_SKILL_DIR/standards"/builder-*.md 2>/dev/null || true
  cp "$STAGING_DIR"/standards/builder-*.md "$CODEX_SKILL_DIR/standards/" 2>/dev/null || true
  cp "$STAGING_DIR/skills/planning/SKILL.md" "$CODEX_SKILL_DIR/references/planning-skill.md"
fi

# Standards
for name in builder-standards.md builder-tdd.md builder-workflow.md builder-contract.md builder-guardrails-implement.md builder-guardrails-review.md builder-guardrails-verify.md; do
  src="$STAGING_DIR/standards/$name"
  [ -f "$src" ] || continue
  dst="$TARGET_ABS/.builder/$name"
  if [ -e "$dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$dst")
  else
    CREATED=$(append_line "$CREATED" "$dst")
  fi
  cp "$src" "$dst"
  # Also install standards into a structured standards/ subdirectory
  mkdir -p "$TARGET_ABS/.builder/standards"
  cp "$src" "$TARGET_ABS/.builder/standards/$name"
done

# The help prompt is BOTH a slash command and a runtime asset: eight load_set declarations across
# specify/design/review/sync/ff name `prompts/isanna-help.prompt.md`, and load_set paths resolve
# under {{BUILDER_ROOT}}. Installing it only to the agent's prompt directory left those eight
# declarations pointing at a file that was not there.
mkdir -p "$TARGET_ABS/.builder/prompts"
if [ -f "$STAGING_DIR/prompts/isanna-help.prompt.md" ]; then
  runtime_help="$TARGET_ABS/.builder/prompts/isanna-help.prompt.md"
  if [ -e "$runtime_help" ]; then
    UPDATED=$(append_line "$UPDATED" "$runtime_help")
  else
    CREATED=$(append_line "$CREATED" "$runtime_help")
  fi
  cp "$STAGING_DIR/prompts/isanna-help.prompt.md" "$runtime_help"
fi

# Scripts (external validator and helpers)
mkdir -p "$TARGET_ABS/.builder/scripts" "$TARGET_ABS/.builder/scripts/_validators" "$TARGET_ABS/.builder/scripts/_telemetry" "$TARGET_ABS/.builder/scripts/_constitution" "$TARGET_ABS/.builder/scripts/_dispatch_runtime" "$TARGET_ABS/.builder/scripts/_sync" "$TARGET_ABS/.builder/schemas"
for script_name in validate-spec.py validate-constitution.py render-spec-artifacts.py record-workflow-event.py analyze-workflow-telemetry.py lint-builder-assets.py check-mirror-drift.py list-specs.py _yaml.py _yaml_compat.py; do
  if [ -f "$STAGING_DIR/scripts/$script_name" ]; then
    if [ -e "$TARGET_ABS/.builder/scripts/$script_name" ]; then
      UPDATED=$(append_line "$UPDATED" "$TARGET_ABS/.builder/scripts/$script_name")
    else
      CREATED=$(append_line "$CREATED" "$TARGET_ABS/.builder/scripts/$script_name")
    fi
    cp "$STAGING_DIR/scripts/$script_name" "$TARGET_ABS/.builder/scripts/$script_name"
    chmod +x "$TARGET_ABS/.builder/scripts/$script_name" 2>/dev/null || true
  fi
done
for validator_src in "$STAGING_DIR"/scripts/_validators/*.py; do
  [ -f "$validator_src" ] || continue
  validator_name=$(basename "$validator_src")
  validator_dst="$TARGET_ABS/.builder/scripts/_validators/$validator_name"
  if [ -e "$validator_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$validator_dst")
  else
    CREATED=$(append_line "$CREATED" "$validator_dst")
  fi
  cp "$validator_src" "$validator_dst"
done
for telemetry_src in "$STAGING_DIR"/scripts/_telemetry/*.py; do
  [ -f "$telemetry_src" ] || continue
  telemetry_name=$(basename "$telemetry_src")
  telemetry_dst="$TARGET_ABS/.builder/scripts/_telemetry/$telemetry_name"
  if [ -e "$telemetry_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$telemetry_dst")
  else
    CREATED=$(append_line "$CREATED" "$telemetry_dst")
  fi
  cp "$telemetry_src" "$telemetry_dst"
done
for constitution_src in "$STAGING_DIR"/scripts/_constitution/*.py; do
  [ -f "$constitution_src" ] || continue
  constitution_name=$(basename "$constitution_src")
  constitution_dst="$TARGET_ABS/.builder/scripts/_constitution/$constitution_name"
  if [ -e "$constitution_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$constitution_dst")
  else
    CREATED=$(append_line "$CREATED" "$constitution_dst")
  fi
  cp "$constitution_src" "$constitution_dst"
done
for dispatch_src in "$STAGING_DIR"/scripts/_dispatch_runtime/*.py; do
  [ -f "$dispatch_src" ] || continue
  dispatch_name=$(basename "$dispatch_src")
  dispatch_dst="$TARGET_ABS/.builder/scripts/_dispatch_runtime/$dispatch_name"
  if [ -e "$dispatch_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$dispatch_dst")
  else
    CREATED=$(append_line "$CREATED" "$dispatch_dst")
  fi
  cp "$dispatch_src" "$dispatch_dst"
done
for sync_src in "$STAGING_DIR"/scripts/_sync/*.py; do
  [ -f "$sync_src" ] || continue
  sync_name=$(basename "$sync_src")
  sync_dst="$TARGET_ABS/.builder/scripts/_sync/$sync_name"
  if [ -e "$sync_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$sync_dst")
  else
    CREATED=$(append_line "$CREATED" "$sync_dst")
  fi
  cp "$sync_src" "$sync_dst"
done
for schema_src in "$STAGING_DIR"/schemas/*.schema.yaml; do
  [ -f "$schema_src" ] || continue
  schema_name=$(basename "$schema_src")
  schema_dst="$TARGET_ABS/.builder/schemas/$schema_name"
  if [ -e "$schema_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$schema_dst")
  else
    CREATED=$(append_line "$CREATED" "$schema_dst")
  fi
  cp "$schema_src" "$schema_dst"
done
for test_src in "$STAGING_DIR"/tests/test_*.sh; do
  [ -f "$test_src" ] || continue
  test_name=$(basename "$test_src")
  test_dst="$TARGET_ABS/.builder/tests/$test_name"
  if [ -e "$test_dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$test_dst")
  else
    CREATED=$(append_line "$CREATED" "$test_dst")
  fi
  cp "$test_src" "$test_dst"
  chmod +x "$test_dst" 2>/dev/null || true
done
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
  echo "[NOTE] python3 not found on PATH (probe: python3 -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)')"
  echo "       The validator at .builder/scripts/validate-spec.py"
  echo "       will be skipped by phase prompts, which will fall back to prose checks."
  echo "       Install Python 3.11+ to enable deterministic schema/evidence validation."
fi

# Templates
for template_name in spec.yaml intent.yaml intent-object.yaml requirements.yaml design.yaml gate-lane-policy.yaml tasks.yaml handoff.yaml setup-decisions.yaml; do
  dst="$TARGET_ABS/.builder/templates/$template_name"
  if [ -e "$dst" ]; then
    UPDATED=$(append_line "$UPDATED" "$dst")
  else
    CREATED=$(append_line "$CREATED" "$dst")
  fi
  cp "$STAGING_DIR/templates/$template_name" "$dst"
done

# Constitution — preserve if exists
if [ -e "$TARGET_ABS/.builder/constitution.md" ]; then
  PRESERVED=$(append_line "$PRESERVED" "$TARGET_ABS/.builder/constitution.md")
else
  cp "$STAGING_DIR/templates/constitution.md" "$TARGET_ABS/.builder/constitution.md"
  CREATED=$(append_line "$CREATED" "$TARGET_ABS/.builder/constitution.md")
fi

# Planning skill — lane-neutral install for every AI target.
# Prompt load_sets (isanna-1..isanna-6) reference `skills/planning/SKILL.md` relative to
# {{BUILDER_ROOT}} (= .builder), so the skill MUST resolve at
# .builder/skills/planning/SKILL.md on copilot, claude, AND codex. This is a
# Builder-owned runtime asset (refreshed on reinstall), distinct from the
# project-owned Copilot-native copy under .github/skills/ below.
mkdir -p "$TARGET_ABS/.builder/skills/planning"
if [ -e "$TARGET_ABS/.builder/skills/planning/SKILL.md" ]; then
  UPDATED=$(append_line "$UPDATED" "$TARGET_ABS/.builder/skills/planning/SKILL.md")
else
  CREATED=$(append_line "$CREATED" "$TARGET_ABS/.builder/skills/planning/SKILL.md")
fi
cp "$STAGING_DIR/skills/planning/SKILL.md" "$TARGET_ABS/.builder/skills/planning/SKILL.md"

# Planning skill (Copilot only)
if [ "$AI_TARGET" = "copilot" ]; then
  if [ -e "$TARGET_ABS/.github/skills/planning/SKILL.md" ]; then
    PRESERVED=$(append_line "$PRESERVED" "$TARGET_ABS/.github/skills/planning/SKILL.md")
  else
    mkdir -p "$TARGET_ABS/.github/skills/planning"
    cp "$STAGING_DIR/skills/planning/SKILL.md" "$TARGET_ABS/.github/skills/planning/SKILL.md"
    CREATED=$(append_line "$CREATED" "$TARGET_ABS/.github/skills/planning/SKILL.md")
  fi
fi

# isanna-builder agent skills — the shipped behavioral skill set, installed into the
# target's NATIVE skills dir (refreshed on every reinstall so each repo learns the
# current .builder/-aware behavior). This is THE fix for the "installed skill set is
# stale" gap: previously nothing shipped these.
case "$AI_TARGET" in
  claude)  BUILDER_SKILLS_BASE="$TARGET_ABS/.claude/skills" ;;
  copilot) BUILDER_SKILLS_BASE="$TARGET_ABS/.github/skills" ;;
  codex)   BUILDER_SKILLS_BASE="$CODEX_HOME_ABS/skills" ;;
  *)       BUILDER_SKILLS_BASE="" ;;
esac
if [ -n "$BUILDER_SKILLS_BASE" ]; then
  for sk in "$STAGING_DIR"/skills/isanna-builder*/; do
    [ -d "$sk" ] || continue
    bn=$(basename "$sk")
    [ -f "$sk/SKILL.md" ] || continue
    mkdir -p "$BUILDER_SKILLS_BASE/$bn"
    bdst="$BUILDER_SKILLS_BASE/$bn/SKILL.md"
    if [ -e "$bdst" ]; then
      UPDATED=$(append_line "$UPDATED" "$bdst")
    else
      CREATED=$(append_line "$CREATED" "$bdst")
    fi
    cp "$sk/SKILL.md" "$bdst"
  done
fi

# INSTALL-STATE
INSTALLED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date '+%Y-%m-%dT%H:%M:%SZ')
BUILDER_REF_DETECTED=$(cd "$SCRIPT_DIR" && git describe --tags --always 2>/dev/null || echo "unknown")
INSTALL_STATE_FILE="$TARGET_ABS/.builder/install-state.json"
mkdir -p "$TARGET_ABS/.builder"

# Build assets map: each installed file gets a sha256 if python3 is available, else "unavailable"
ASSETS_JSON=""
ASSET_SOURCE_DIR="$STAGING_DIR"
sha256_of() {
  file="$1"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
    python3 -c "import hashlib,sys; h=hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest(); print(h)" "$file" 2>/dev/null || echo "unavailable"
  else
    echo "unavailable"
  fi
}
add_asset() {
  rel_path="$1"   # path relative to install root
  src_file="$2"   # canonical source file (absolute)
  src_rel="$3"    # source path relative to builder root
  [ -f "$src_file" ] || return
  digest=$(sha256_of "$src_file")
  entry="    \"$rel_path\": {\"source\": \"$src_rel\", \"sha256\": \"$digest\"}"
  if [ -z "$ASSETS_JSON" ]; then
    ASSETS_JSON="$entry"
  else
    ASSETS_JSON="$ASSETS_JSON,
$entry"
  fi
}

# Register prompts
if [ -n "$PROMPT_DIR" ]; then
  for src in "$ASSET_SOURCE_DIR"/prompts/isanna-*.prompt.md "$ASSET_SOURCE_DIR/prompts/builder-handoff-template.prompt.md"; do
    [ -f "$src" ] || continue
    base=$(basename "$src")
    if [ "$AI_TARGET" = "claude" ]; then
      installed_base=$(echo "$base" | sed 's/\.prompt\.md$/.md/')
    else
      installed_base="$base"
    fi
    add_asset "$PROMPT_DIR/$installed_base" "$src" "prompts/$base"
  done
fi
# Register standards
for name in builder-standards.md builder-tdd.md builder-workflow.md builder-contract.md builder-guardrails-implement.md builder-guardrails-review.md builder-guardrails-verify.md; do
  src="$ASSET_SOURCE_DIR/standards/$name"
  [ -f "$src" ] || continue
  add_asset ".builder/$name" "$src" "standards/$name"
  add_asset ".builder/standards/$name" "$src" "standards/$name"
done
# Register scripts
for name in validate-spec.py validate-constitution.py render-spec-artifacts.py record-workflow-event.py analyze-workflow-telemetry.py lint-builder-assets.py check-mirror-drift.py list-specs.py _yaml.py _yaml_compat.py; do
  src="$ASSET_SOURCE_DIR/scripts/$name"
  [ -f "$src" ] || continue
  add_asset ".builder/scripts/$name" "$src" "scripts/$name"
done
for src in "$ASSET_SOURCE_DIR"/scripts/_validators/*.py; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/scripts/_validators/$name" "$src" "scripts/_validators/$name"
done
for src in "$ASSET_SOURCE_DIR"/scripts/_telemetry/*.py; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/scripts/_telemetry/$name" "$src" "scripts/_telemetry/$name"
done
for src in "$ASSET_SOURCE_DIR"/scripts/_constitution/*.py; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/scripts/_constitution/$name" "$src" "scripts/_constitution/$name"
done
for src in "$ASSET_SOURCE_DIR"/scripts/_dispatch_runtime/*.py; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/scripts/_dispatch_runtime/$name" "$src" "scripts/_dispatch_runtime/$name"
done
for src in "$ASSET_SOURCE_DIR"/scripts/_sync/*.py; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/scripts/_sync/$name" "$src" "scripts/_sync/$name"
done
for src in "$ASSET_SOURCE_DIR"/schemas/*.schema.yaml; do
  [ -f "$src" ] || continue
  name=$(basename "$src")
  add_asset ".builder/schemas/$name" "$src" "schemas/$name"
done
# NOT installed: this project's own shell tests. They exercise the SOURCE repo -- install.sh,
# the standalone-installer build, scripts/ and tests/fixtures/ -- none of which exists in an
# installed project, so they cannot pass there. Run them from a clone with `make shell-tests`.
# Register planning skill — lane-neutral copy resolved by prompt load_sets
if [ -f "$ASSET_SOURCE_DIR/skills/planning/SKILL.md" ]; then
  add_asset ".builder/skills/planning/SKILL.md" "$ASSET_SOURCE_DIR/skills/planning/SKILL.md" "skills/planning/SKILL.md"
fi
# Register skill
if [ "$AI_TARGET" = "copilot" ] && [ -f "$ASSET_SOURCE_DIR/skills/planning/SKILL.md" ]; then
  add_asset ".github/skills/planning/SKILL.md" "$ASSET_SOURCE_DIR/skills/planning/SKILL.md" "skills/planning/SKILL.md"
fi
# Register every native isanna-builder skill.  These are refreshed on every
# install, so each one must participate in integrity tracking as well.
if [ -n "$BUILDER_SKILLS_BASE" ]; then
  builder_rel_base=${BUILDER_SKILLS_BASE#"$TARGET_ABS"/}
  for src in "$ASSET_SOURCE_DIR"/skills/isanna-builder*/SKILL.md; do
    [ -f "$src" ] || continue
    skill_name=$(basename "$(dirname "$src")")
    add_asset "$builder_rel_base/$skill_name/SKILL.md" "$src" "skills/$skill_name/SKILL.md"
  done
fi
if [ "$AI_TARGET" = "codex" ] && [ -f "$ASSET_SOURCE_DIR/skills/builder/SKILL.md" ]; then
  add_asset "$CODEX_SKILL_DIR/SKILL.md" "$ASSET_SOURCE_DIR/skills/builder/SKILL.md" "skills/builder/SKILL.md"
  [ -f "$ASSET_SOURCE_DIR/skills/builder/agents/openai.yaml" ] && add_asset "$CODEX_SKILL_DIR/agents/openai.yaml" "$ASSET_SOURCE_DIR/skills/builder/agents/openai.yaml" "skills/builder/agents/openai.yaml"
  for src in "$ASSET_SOURCE_DIR"/prompts/isanna-*.prompt.md "$ASSET_SOURCE_DIR/prompts/builder-handoff-template.prompt.md"; do
    [ -f "$src" ] || continue
    base=$(basename "$src")
    add_asset "$CODEX_SKILL_DIR/prompts/$base" "$src" "prompts/$base"
  done
  for name in builder-standards.md builder-tdd.md builder-workflow.md builder-contract.md builder-guardrails-implement.md builder-guardrails-review.md builder-guardrails-verify.md; do
    src="$ASSET_SOURCE_DIR/standards/$name"
    [ -f "$src" ] || continue
    add_asset "$CODEX_SKILL_DIR/standards/$name" "$src" "standards/$name"
  done
  add_asset "$CODEX_SKILL_DIR/references/planning-skill.md" "$ASSET_SOURCE_DIR/skills/planning/SKILL.md" "skills/planning/SKILL.md"
fi
# Register templates.  Constitution is installed at the .builder root and
# may be project-preserved, so hash its actual installed contents.
for name in spec.yaml intent.yaml intent-object.yaml requirements.yaml design.yaml gate-lane-policy.yaml tasks.yaml handoff.yaml setup-decisions.yaml; do
  src="$ASSET_SOURCE_DIR/templates/$name"
  [ -f "$src" ] || continue
  add_asset ".builder/templates/$name" "$src" "templates/$name"
done
if [ -f "$TARGET_ABS/.builder/constitution.md" ]; then
  add_asset ".builder/constitution.md" "$TARGET_ABS/.builder/constitution.md" "templates/constitution.md"
fi

cat > "$INSTALL_STATE_FILE" <<INSTALL_STATE_EOF
{
  "builder_ref": "${BUILDER_REF:-$BUILDER_REF_DETECTED}",
$(if [ "${BUILDER_INSTALL_PROVENANCE:-}" = "standalone" ]; then
  printf '  "provenance": "standalone",\n'
fi)
$(if [ "$AI_TARGET" = "codex" ]; then
  printf '  "codex_skill_dir": "%s",\n' "$CODEX_SKILL_DIR"
fi)
  "installed_at": "$INSTALLED_AT",
  "assets": {
${ASSETS_JSON}
  }
}
INSTALL_STATE_EOF
echo "[STATE] Wrote install-state.json to $INSTALL_STATE_FILE"

# REPORT
echo "[REPORT] Install complete (target: $AI_TARGET)"
echo "[REPORT] Created files:"
print_lines "$CREATED"
echo "[REPORT] Updated files:"
print_lines "$UPDATED"
echo "[REPORT] Removed files:"
print_lines "$REMOVED"
echo "[REPORT] Preserved files:"
print_lines "$PRESERVED"
echo ""
  echo "isanna-builder ${BUILDER_REF:-$BUILDER_REF_DETECTED} installed for $AI_TARGET${PROMPT_DIR:+ → prompts in $PROMPT_DIR}"
if [ "$AI_TARGET" = "copilot" ]; then
  echo "Next steps:"
  echo "  1. Reload your VS Code window (Ctrl+Shift+P → 'Developer: Reload Window')"
  echo "  2. In Copilot Chat, run /isanna-setup for guided project configuration"
  echo "  3. Start your first spec:  /isanna-1-specify <what you want to build>"
  echo "     (run /isanna-help anytime for the full command reference)"
elif [ "$AI_TARGET" = "codex" ]; then
  echo "Next steps:"
  echo "  1. Restart Codex or open a new session so the isanna-builder skill is discovered"
  echo "  2. Ask Codex to use isanna-builder /isanna-setup for guided project configuration"
  echo "  3. Start your first spec: ask Codex to run isanna-builder /isanna-1-specify <what you want to build>"
  echo "     (isanna-builder /isanna-help lists the full command reference)"
else
  echo "Next steps:"
  echo "  1. Run /isanna-setup for guided project configuration"
  echo "  2. Start your first spec:  /isanna-1-specify <what you want to build>"
  echo "     (run /isanna-help anytime for the full command reference)"
fi
if [ ! -d "$TARGET_ABS/.builder-home" ]; then
  echo ""
  echo "Optional — Builder Home (a portfolio view across several builder repos):"
  echo "  Needs the 'isanna' CLI, which ships with the builder repository, not with this"
  echo "  installer. From a clone of https://github.com/isanna-ai/builder:"
  # Quoted: this line is printed to be COPIED, and a target path with a space in it (common on
  # macOS -- "~/My Projects") otherwise pastes as two arguments and takes the wrong one.
  echo "    isanna home init --projects-root '$TARGET_ABS' --confirm"
  echo "  Nothing is created until you re-run it with --confirm."
fi

cleanup
