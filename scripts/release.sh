#!/usr/bin/env bash
# Usage: ./scripts/release.sh [patch|minor|major|X.Y.Z]
#
# Bumps version in pyproject.toml AND package.json, commits, tags, and pushes.
# Pushing the tag triggers the publish workflow (PyPI + npm).
#
# IMPORTANT: pyproject.toml version MUST match the tag — the workflow enforces this.
#
# Examples:
#   ./scripts/release.sh patch        # 0.6.2 → 0.6.3
#   ./scripts/release.sh minor        # 0.6.2 → 0.7.0
#   ./scripts/release.sh 1.0.0        # set explicit version

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUMP="${1:-patch}"

# ── use existing bump_version.py for the actual bump ─────────────────────────

BUMP_SCRIPT="$REPO_ROOT/scripts/bump_version.py"

if [[ ! -f "$BUMP_SCRIPT" ]]; then
    echo "ERROR: $BUMP_SCRIPT not found" >&2
    exit 1
fi

# Dry-run first to show what will happen
echo "Preview:"
python3 "$BUMP_SCRIPT" "$BUMP" --pkg "$REPO_ROOT" --dry-run
echo ""

# Confirm
read -rp "Proceed? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

# Get new version (run bump to get the number, without committing yet)
NEW_VERSION=$(python3 -c "
import re, sys
sys.path.insert(0, '$REPO_ROOT/scripts')
# Parse manually — same logic as bump_version.py
text = open('$REPO_ROOT/pyproject.toml').read()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.MULTILINE)
current = m.group(1)
part = '$BUMP'
major, minor, patch = map(int, current.split('.'))
if part == 'major':
    print(f'{major+1}.0.0')
elif part == 'minor':
    print(f'{major}.{minor+1}.0')
elif part == 'patch':
    print(f'{major}.{minor}.{patch+1}')
else:
    parts = part.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f'Invalid version: {part!r}')
    print(part)
")

# Bump pyproject.toml and commit via bump_version.py
python3 "$BUMP_SCRIPT" "$BUMP" --pkg "$REPO_ROOT"

# Sync package.json to same version (workflow also does this, but keep file in sync)
PACKAGE_JSON="$REPO_ROOT/package.json"
python3 -c "
import json
with open('$PACKAGE_JSON') as f:
    pkg = json.load(f)
pkg['version'] = '$NEW_VERSION'
with open('$PACKAGE_JSON', 'w') as f:
    json.dump(pkg, f, indent=2)
    f.write('\n')
print(f'  Updated package.json to $NEW_VERSION')
"

cd "$REPO_ROOT"
git add package.json
git commit --amend --no-edit
echo "  Amended commit to include package.json"

TAG="v$NEW_VERSION"
git tag -a "$TAG" -m "Release $TAG"
echo "  Tagged: $TAG"

echo ""
echo "Pushing commit and tag to origin..."
git push
git push --tags

echo ""
echo "Done. GitHub Actions will now:"
echo "  1. Run tests"
echo "  2. Verify pyproject.toml version matches tag ($TAG)"
echo "  3. Publish skcapstone $NEW_VERSION to PyPI"
echo "  4. Publish @smilintux/skcapstone $NEW_VERSION to npm"
