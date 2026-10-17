#!/usr/bin/env bash
# Write versions.json and a redirecting index.html at the root of the local
# hosting dir, matching the gh-pages layout the theme's version picker expects.
#
#   versions.sh <www_dir>
set -euo pipefail

WWW=${1:?www_dir}
cd "$WWW"
{
  [ -d latest ] && echo latest
  find . -mindepth 1 -maxdepth 1 -type d -name 'v*' -printf '%f\n' | sort -rV
} | jq -R '{tag: .}' | jq -s '.[0].latest = true' > versions.json

echo '<meta http-equiv="refresh" content="0;url=latest/">' > index.html
touch .nojekyll   # GitHub Pages: serve files as-is
echo "Versions: $(jq -r 'map(.tag) | join(", ")' versions.json)"
