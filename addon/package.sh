#!/bin/bash
# Build ci_buddy.ankiaddon — a zip of the ci_buddy/ package contents with
# manifest.json at the ZIP ROOT (AnkiWeb requirement; the files must NOT be
# nested inside a folder). No wheels, no vendoring — stdlib + aqt/anki only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDON_DIR="ci_buddy"
OUTPUT="ci_buddy.ankiaddon"

# Reproducible builds: freeze every archived entry to a fixed timestamp so two
# builds of the same tree are byte-identical. `touch -t` format: CCYYMMDDhhmm.SS
# (portable across macOS/BSD and GNU). Override via ZIP_MTIME if you need to.
ZIP_MTIME="${ZIP_MTIME:-202101010000.00}"

cd "$HERE"

if [ ! -f "$ADDON_DIR/manifest.json" ]; then
    echo "ERROR: $ADDON_DIR/manifest.json not found" >&2
    exit 1
fi

echo "=== Cleaning previous build ==="
rm -f "$OUTPUT"

echo "=== Staging package (normalized mtimes) ==="
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# Copy the package *contents* into a clean staging dir, excluding caches/cruft,
# in sorted order so the archive's member order is deterministic.
( cd "$ADDON_DIR" && find . -type f \
    ! -name '*.pyc' \
    ! -path '*/__pycache__/*' \
    ! -name '.DS_Store' \
    ! -name '*.git*' \
    | sort | while IFS= read -r f; do
        mkdir -p "$STAGE/$(dirname "$f")"
        cp "$f" "$STAGE/$f"
    done )
# Freeze mtimes so the zip's stored timestamps are stable across builds.
find "$STAGE" -exec touch -t "$ZIP_MTIME" {} +

echo "=== Creating $OUTPUT (manifest at zip root, reproducible) ==="
# -X strips extra attributes (uid/gid, extended attrs); a sorted, explicit file
# list keeps member order deterministic. Combined with the frozen mtimes above,
# repeated builds of the same tree are byte-identical.
( cd "$STAGE" && find . -type f | sort | zip -X -q "$HERE/$OUTPUT" -@ )

echo ""
echo "=== Build complete ==="
echo "Output: $HERE/$OUTPUT"
echo "Size:   $(du -h "$OUTPUT" | cut -f1)"
echo ""
echo "Contents (manifest.json must be at root):"
unzip -l "$OUTPUT"
