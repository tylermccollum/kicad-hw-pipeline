#!/usr/bin/env bash
# One-time setup for a project created from the KiCad template. Safe to re-run.
#
#   make -C site init            (TITLE="My Board" to set the site title)
#
# 1. git init, if this isn't a repository yet
# 2. install .github/workflows/ci.yml from site/ci/ci.yml (KiCad templates
#    don't copy hidden directories)
# 3. fill the __PROJECT_TITLE__ / __REPO_URL__ placeholders in site/hugo.yaml
#    and README.md (the template's README is replaced by a project README)
#    (the URL comes from the `origin` remote; re-run after adding one)
# 4. add the (empty) drawing layers to the board for your own annotations;
#    the drawing layout itself is generated at build time
set -euo pipefail

SITE=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(dirname "$SITE")
cd "$REPO"

pros=(*.kicad_pro)
[ ${#pros[@]} -eq 1 ] && [ -f "${pros[0]}" ] || { echo "ERROR: expected one .kicad_pro in $REPO" >&2; exit 1; }
PROJECT=${PROJECT:-$(basename "${pros[0]}" .kicad_pro)}

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  git init -q
  echo "+ git init"
fi

mkdir -p .github/workflows
if ! cmp -s site/ci/ci.yml .github/workflows/ci.yml; then
  cp site/ci/ci.yml .github/workflows/ci.yml
  echo "+ .github/workflows/ci.yml"
fi

if grep -q 'hw-pipeline template README' README.md 2>/dev/null && [ -f site/README.project.md ]; then
  mv site/README.project.md README.md
  echo "+ README.md (project README)"
fi
for f in site/hugo.yaml README.md; do
  if [ -f "$f" ] && grep -q '__PROJECT_TITLE__' "$f"; then
    TITLE=${TITLE:-$PROJECT}
    sed -i "s|__PROJECT_TITLE__|${TITLE//|/\\|}|g" "$f"
    echo "+ $f: title \"$TITLE\""
  fi
done
if grep -q '__REPO_URL__' site/hugo.yaml; then
  remote=$(git remote get-url origin 2>/dev/null || true)
  case "$remote" in
    git@*:*) host=${remote#git@}; url="https://${host%%:*}/${remote#*:}" ;;
    https://*) url="$remote" ;;
    *) url="" ;;
  esac
  url=${url%.git}
  if [ -n "$url" ]; then
    sed -i "s|__REPO_URL__|$url|g" site/hugo.yaml
    echo "+ site/hugo.yaml: repo $url"
  else
    echo "NOTE: no 'origin' remote yet; re-run 'make -C site init' after adding one (or edit site/hugo.yaml)"
  fi
fi

if ls "~$PROJECT.kicad_pcb.lck" >/dev/null 2>&1; then
  echo "NOTE: board is open in KiCad; skipped adding the drawing layers (re-run init after closing it)"
else
  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$REPO:/w" -w /w \
    "${KIBOT_IMAGE:-ghcr.io/inti-cmnb/kicad10_auto_full@sha256:7b720dc99b2775e4f4133f06887cf9a53c85a8403438d6684ecf0916d124f52a}" \
    python3 site/scripts/setup_drawing_layers.py "$PROJECT.kicad_pcb" "$PROJECT.kibot.yaml" --layers-only 2>&1 | grep -v assert || true
fi
echo "Done. Next: make -C site kibot build host"
