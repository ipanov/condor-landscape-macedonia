#!/usr/bin/env bash
# Condor-landscape completeness gate (Stop hook).
#
# Runs scripts/verify_landscape.py for every installed Condor landscape that is
# "in play" (the repo has uncommitted changes), so an INCOMPLETE or STALE
# landscape -- e.g. a NorthMacedonia shipped without its fresh .bmp/.tdm/.cup --
# cannot be quietly declared done. On any failure it exits 2, which feeds the
# checklist back to Claude (Stop-hook blocking convention).
#
# GENERIC: it discovers landscapes by scanning C:/Condor2/Landscapes/* for a
# <Name>/<Name>.trn, and maps each to its CONDOR_LANDSCAPE selector. To override
# the discovered set (e.g. only gate certain landscapes), list landscape *folder
# names*, one per line, in .claude/hooks/landscapes.txt.
#
# Fast: uses verify_landscape.py --metadata-only (the load-critical + freshness
# subset: .ini/.trn/.tr3/.apt/.cup/.tdm/.bmp/.obj/hashes/Images, with .bmp/.tdm/
# .cup freshness vs .trn/.apt). The heavy .dds/.for full gate is left to an
# explicit `python scripts/verify_landscape.py` run.
#
# Triggers: on the Stop event (Claude finishing a turn). Configured in
# .claude/settings.json. Hooks load at session start -- restart Claude after edits.
set -uo pipefail

# Resolve repo root from this script's location (.claude/hooks/ -> repo root).
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HOOK_DIR/../.." && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$REPO_ROOT}"
LANDSCAPES_ROOT="C:/Condor2/Landscapes"
PYTHON="${CONDOR_PY:-python}"

cd "$PROJECT_DIR" || exit 0

# Consume stdin (hook receives JSON; we don't need its fields).
cat >/dev/null 2>&1 || true

# Only gate when there are uncommitted changes (don't nag on a clean tree).
if git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -z "$(git -C "$PROJECT_DIR" status --porcelain 2>/dev/null)" ]; then
    exit 0
  fi
else
  exit 0
fi

# Map a landscape FOLDER NAME to its CONDOR_LANDSCAPE selector.
selector_for() {
  case "$1" in
    NorthMacedonia) echo "nm" ;;
    MacedoniaSkopje) echo "skopje" ;;
    # Default: lowercase the folder name. condor_grid only special-cases the two
    # above today; extend condor_grid + this case together for new landscapes.
    *) echo "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" ;;
  esac
}

# Build the list of landscape folders to check.
declare -a LANDSCAPES=()
OVERRIDE="$HOOK_DIR/landscapes.txt"
if [ -f "$OVERRIDE" ]; then
  while IFS= read -r line; do
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -n "$line" ] && LANDSCAPES+=("$line")
  done < "$OVERRIDE"
else
  if [ -d "$LANDSCAPES_ROOT" ]; then
    for d in "$LANDSCAPES_ROOT"/*/; do
      name="$(basename "$d")"
      # A real landscape has <Name>/<Name>.trn; skip backup/junk dirs.
      [ -f "$d/$name.trn" ] && LANDSCAPES+=("$name")
    done
  fi
fi

if [ "${#LANDSCAPES[@]}" -eq 0 ]; then
  exit 0
fi

FAILED=()
REPORT=""
for name in "${LANDSCAPES[@]}"; do
  sel="$(selector_for "$name")"
  out="$(CONDOR_LANDSCAPE="$sel" "$PYTHON" "$PROJECT_DIR/scripts/verify_landscape.py" --metadata-only 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    FAILED+=("$name")
    # Keep only the FAIL lines + the RESULT line for a compact message.
    fails="$(printf '%s\n' "$out" | grep -E '\[FAIL\]|RESULT:' || true)"
    REPORT="${REPORT}
=== ${name} (CONDOR_LANDSCAPE=${sel}) ===
${fails}"
  fi
done

if [ "${#FAILED[@]}" -gt 0 ]; then
  {
    echo "Condor-landscape completeness gate FAILED for: ${FAILED[*]}"
    echo "A landscape is NOT done until verify_landscape passes (and the flight"
    echo "planner has been opened in-sim). Regenerate the failed/STALE artifacts:"
    echo "  CONDOR_LANDSCAPE=<sel> python scripts/build_landscape.py   # metadata"
    echo "then re-run: CONDOR_LANDSCAPE=<sel> python scripts/verify_landscape.py"
    echo "${REPORT}"
  } >&2
  exit 2
fi

exit 0
