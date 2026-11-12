#!/usr/bin/env bash
# Upload every video in schedule.json to the GitHub Release "videos" of THIS repo, as <id>.mp4.
# Safe to run again: videos already uploaded with the right size are skipped; changed ones are replaced.
#
# Needs: the gh CLI, logged in (gh auth login), and this folder pushed to the private GitHub repo.
# Usage:  ./upload_videos.sh                 (videos from ../Instagram/videos)
#         VIDEOS_DIR=/some/folder ./upload_videos.sh
#         ./upload_videos.sh --repo OWNER/REPO
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
VIDEOS_DIR="${VIDEOS_DIR:-$HERE/../Instagram/videos}"
TAG="videos"
REPO_ARGS=()
if [ "${1:-}" = "--repo" ] && [ -n "${2:-}" ]; then
  REPO_ARGS=(--repo "$2")
fi

command -v gh >/dev/null || { echo "The gh CLI is not installed (brew install gh)."; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not logged in. Run: gh auth login"; exit 1; }
[ -f schedule.json ] || { echo "schedule.json is missing. On the Mac run: python -m autopilot cloud export"; exit 1; }

# The repo is public (GitHub's 2026 private-repo Actions bug): the videos go public on Instagram anyway.
VISIBILITY="$(gh repo view "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" --json visibility -q .visibility)"
echo "Repo visibility: $VISIBILITY"

if ! gh release view "$TAG" "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" >/dev/null 2>&1; then
  echo "Creating the release '$TAG'..."
  gh release create "$TAG" "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" --title "Videos" \
    --notes "Reel videos for the cloud poster. Do not delete." --latest=false
fi

# "name size" of every asset already there.
EXISTING="$(gh release view "$TAG" "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" --json assets -q '.assets[] | "\(.name) \(.size)"')"

# "id asset" for every schedule item (python3 is on every Mac).
ITEMS="$(python3 -I -c '
import json, sys
raw = json.load(open("schedule.json", encoding="utf-8"))
items = raw["items"] if isinstance(raw, dict) else raw
for it in items:
    print(it["id"], it.get("asset") or (it["id"] + ".mp4"))
')"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
uploaded=0; skipped=0; missing=0

while read -r ID ASSET; do
  [ -n "$ID" ] || continue
  SRC="$VIDEOS_DIR/$ID/${ID}_instagram.mp4"
  if [ ! -f "$SRC" ]; then
    echo "MISSING  $ID  ($SRC not found)"; missing=$((missing + 1)); continue
  fi
  SIZE="$(stat -f %z "$SRC" 2>/dev/null || stat -c %s "$SRC")"
  HAVE="$(printf '%s\n' "$EXISTING" | awk -v n="$ASSET" '$1 == n {print $2}')"
  if [ "$HAVE" = "$SIZE" ]; then
    echo "skip     $ASSET (already uploaded)"; skipped=$((skipped + 1)); continue
  fi
  ln -sf "$SRC" "$STAGE/$ASSET"          # the asset is named after the link: <id>.mp4
  echo "upload   $ASSET ($((SIZE / 1048576)) MB)..."
  CLOBBER=()
  [ -n "$HAVE" ] && CLOBBER=(--clobber)   # same name, different size: replace the old upload
  gh release upload "$TAG" "$STAGE/$ASSET" "${REPO_ARGS[@]+"${REPO_ARGS[@]}"}" "${CLOBBER[@]+"${CLOBBER[@]}"}"
  uploaded=$((uploaded + 1))
done <<< "$ITEMS"

echo
echo "Done: $uploaded uploaded, $skipped already there, $missing missing."
[ "$missing" -eq 0 ] || exit 1
