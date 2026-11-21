#!/usr/bin/env bash
# End-to-end smoke test against one real public URL. Manual only — NOT part of CI.
#
#   scripts/smoke_test.sh [URL]
#
# Uses a throwaway REELSCRIBE_HOME and a throwaway PostgreSQL schema inside the
# TEST database, so it never touches your real queue. Requires
# HOMELAB_TEST_DATABASE_URL (the homelab_test database; never the real one).
set -euo pipefail

: "${HOMELAB_TEST_DATABASE_URL:?set HOMELAB_TEST_DATABASE_URL (test database URL)}"
SCHEMA="smoke_$$"
psql "$HOMELAB_TEST_DATABASE_URL" -qc "CREATE SCHEMA $SCHEMA"
trap 'psql "$HOMELAB_TEST_DATABASE_URL" -qc "DROP SCHEMA $SCHEMA CASCADE"' EXIT
SEP='?'; [[ "$HOMELAB_TEST_DATABASE_URL" == *\?* ]] && SEP='&'
export REELSCRIBE_DATABASE_URL="${HOMELAB_TEST_DATABASE_URL}${SEP}options=-c%20search_path%3D${SCHEMA}"

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
echo "=== database row ==="
psql "$HOMELAB_TEST_DATABASE_URL" -c "SELECT r.shortcode, r.status, length(t.text) AS chars FROM $SCHEMA.reel r LEFT JOIN $SCHEMA.transcript t ON t.reel_id = r.id"
echo
echo "=== resume check (retry + run, audio must not re-download) ==="
reelscribe retry
reelscribe run
echo
echo "smoke test OK — artifacts in $REELSCRIBE_HOME"
