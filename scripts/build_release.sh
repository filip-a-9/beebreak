#!/usr/bin/env bash
# Build the release bundle in dist/ (gitignored):
#   *.whl              wheels for shared, agent_bridge and companion
#   SHA256SUMS         checksums of the wheels
#   marketplace/       Claude Code marketplace holding the plugin
#   beebreak-release.tar.gz   all of the above, for handing to testers
# Testers unpack the tarball and follow the README Install section.
set -euo pipefail

cd "$(dirname "$0")/.."
out="${BEEBREAK_DIST_DIR:-dist}"

rm -rf "$out"
for pkg in beebreak-shared beebreak-agent-bridge beebreak-companion; do
  uv build --wheel --out-dir "$out" --package "$pkg"
done
(cd "$out" && sha256sum -- *.whl > SHA256SUMS)

mkdir -p "$out/marketplace/.claude-plugin" "$out/marketplace/integrations"
cp .claude-plugin/marketplace.json "$out/marketplace/.claude-plugin/"
cp -r integrations/claude-code "$out/marketplace/integrations/"

tmp="$(mktemp -d)"
tar -C "$out" -czf "$tmp/beebreak-release.tar.gz" .
mv "$tmp/beebreak-release.tar.gz" "$out/"
rmdir "$tmp"

echo "built:"
ls -1 "$out"
