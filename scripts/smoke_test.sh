#!/usr/bin/env bash
# End-to-end smoke test against one real public URL. Manual only — NOT part of CI.
#
#   scripts/smoke_test.sh [URL]
#
# Uses a throwaway REELSCRIBE_HOME so it never touches your real queue.
set -euo pipefail

URL="${1:-https://www.instagram.com/reel/Da0H8Qzh5Mk/}"
export REELSCRIBE_HOME="$(mktemp -d)"
export REELSCRIBE_MODEL="${REELSCRIBE_MODEL:-small}"

echo "REELSCRIBE_HOME=$REELSCRIBE_HOME"
echo "URL=$URL"
echo

reelscribe add "$URL"
reelscribe run
echo
echo "=== transcript ==="
cat "$REELSCRIBE_HOME"/transcripts/*.md
echo
echo "=== resume check (retry + run, audio must not re-download) ==="
reelscribe retry
reelscribe run
echo
echo "smoke test OK — artifacts in $REELSCRIBE_HOME"
