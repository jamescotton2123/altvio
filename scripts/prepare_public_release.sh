#!/usr/bin/env bash
# prepare_public_release.sh
#
# Creates a clean public-release copy of Altvio at ../altvio-public-release/
# with fresh git history, excluded commercial/internal files, and sanitized branding.
#
# Usage:
#   ./scripts/prepare_public_release.sh
#   cd ../altvio-public-release && git init && git add . && git commit -m "Initial public release"
#   git remote add origin git@github.com:jamescotton2123/altvio.git
#   git push -u origin main
#
# Review MANIFEST.txt before pushing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_ROOT="$(cd "$SRC_ROOT/.." && pwd)/altvio-public-release"

echo "==> Source: $SRC_ROOT"
echo "==> Dest:   $DEST_ROOT"

if [[ -d "$DEST_ROOT" ]]; then
  echo "ERROR: $DEST_ROOT already exists. Remove or rename it first."
  exit 1
fi

mkdir -p "$DEST_ROOT"

# rsync with exclusions (portable: use rsync if available, else cp)
RSYNC_EXCLUDES=(
  --exclude '.git'
  --exclude '.env'
  --exclude '.venv'
  --exclude 'venv'
  --exclude '__pycache__'
  --exclude '.pytest_cache'
  --exclude '.ruff_cache'
  --exclude '.cursor'
  --exclude '_internal'
  --exclude 'backups'
  --exclude 'data'
  --exclude 'exports'
  --exclude 'BUSINESS_MODEL.md'
  --exclude 'PLATFORM_PLAN.md'
  --exclude 'scripts/raw_orion_export.csv'
  --exclude 'supabase/.temp'
  --exclude '*.csv'
  --exclude '*.xlsx'
  --exclude '*.pdf'
)

if command -v rsync >/dev/null 2>&1; then
  rsync -a "${RSYNC_EXCLUDES[@]}" "$SRC_ROOT/" "$DEST_ROOT/"
else
  echo "WARN: rsync not found; using cp -R (manual cleanup may be needed)"
  cp -R "$SRC_ROOT/." "$DEST_ROOT/"
  rm -rf "$DEST_ROOT/.git" "$DEST_ROOT/.env" "$DEST_ROOT/.venv" "$DEST_ROOT/_internal" \
    "$DEST_ROOT/BUSINESS_MODEL.md" "$DEST_ROOT/PLATFORM_PLAN.md" 2>/dev/null || true
fi

# Use public README
cp "$SRC_ROOT/README.public.md" "$DEST_ROOT/README.md"

# Apply branding sanitization in public copy
if [[ "$(uname)" == "Darwin" ]]; then
  SED_INPLACE=(-i '')
else
  SED_INPLACE=(-i)
fi

# pivot-ops-pro references → generic
if [[ -f "$DEST_ROOT/core/kyc_parser.py" ]]; then
  sed "${SED_INPLACE[@]}" 's|https://pivot-ops-pro.com|https://example.com|g' "$DEST_ROOT/core/kyc_parser.py"
fi
if [[ -f "$DEST_ROOT/api/routes/kyc_webhook.py" ]]; then
  sed "${SED_INPLACE[@]}" 's|pivot-ops-pro .env|local .env|g' "$DEST_ROOT/api/routes/kyc_webhook.py"
fi

# Rename intake header (optional branding cleanup)
if [[ -f "$DEST_ROOT/core/auth.py" ]]; then
  sed "${SED_INPLACE[@]}" 's|X-Pivot-Intake-Key|X-Intake-Key|g' "$DEST_ROOT/core/auth.py"
fi
if [[ -f "$DEST_ROOT/tests/test_intake_auth.py" ]]; then
  sed "${SED_INPLACE[@]}" 's|X-Pivot-Intake-Key|X-Intake-Key|g' "$DEST_ROOT/tests/test_intake_auth.py"
fi

# Write manifest
MANIFEST="$DEST_ROOT/MANIFEST.txt"
{
  echo "Altvio public release manifest"
  echo "Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "Source: $SRC_ROOT"
  echo ""
  echo "EXCLUDED from this copy:"
  echo "  - BUSINESS_MODEL.md, PLATFORM_PLAN.md, _internal/, .env, .git history"
  echo "  - backups/, data/, exports/, supabase/.temp/"
  echo ""
  echo "SANITIZED in this copy:"
  echo "  - README.public.md -> README.md"
  echo "  - pivot-ops-pro.com -> example.com (kyc_parser.py)"
  echo "  - X-Pivot-Intake-Key -> X-Intake-Key (auth.py, tests)"
  echo ""
  echo "PRE-PUSH CHECKLIST:"
  echo "  [ ] rg -i 'manhattan west|steelpeak|sk-' . (no hits)"
  echo "  [ ] No .env file present"
  echo "  [ ] LICENSE file present"
  echo "  [ ] pytest tests/ passes"
  echo ""
  echo "FILE COUNT: $(find "$DEST_ROOT" -type f | wc -l | tr -d ' ')"
} > "$MANIFEST"

echo ""
echo "==> Public release prepared at: $DEST_ROOT"
echo "==> Review MANIFEST.txt, then:"
echo "    cd $DEST_ROOT"
echo "    git init"
echo "    git add ."
echo "    git commit -m \"Initial public release: Altvio alt-investments ops platform\""
echo "    git branch -M main"
echo "    git remote add origin git@github.com:jamescotton2123/altvio.git"
echo "    git push -u origin main"
