#!/usr/bin/env bash
# Stage KiBot outputs into site/static and write site/config_override.yaml.
# Mirrors the "Discover" and "Build Hugo config override" steps of
# laenzlinger/hugo-kicad-site's kicad-site.yml so local and CI builds match.
#
#   prepare.sh <gen_dir> <kicad_src_dir> <version> <base_url>
#
# <version> is "latest" for working-tree builds or a tag like v1.0.0.
set -euo pipefail

GEN=$(cd "${1:?gen_dir}" && pwd)
SRC=$(cd "${2:?kicad_src_dir}" && pwd)
TAG=${3:?version}
BASE_URL=${4:?base_url}
if [ -z "${SCHEMATIC:-}" ]; then
  pro=$(ls "$SRC"/*.kicad_pro | head -1)
  SCHEMATIC=$(basename "$pro" .kicad_pro).kicad_sch
fi
SITE=$(cd "$(dirname "$0")/.." && pwd)
STATIC="$SITE/static"

# ── Static assets ───────────────────────────────────────────
# Only clear what a previous run staged; hand-added static files stay.
for d in "$GEN"/* kicad assembly; do rm -rf "${STATIC:?}/$(basename "$d")"; done
mkdir -p "$STATIC/kicad"
rsync -a --exclude '*_pcb3d/' --exclude '*.pcb3d' "$GEN/" "$STATIC/"
rm -f "$STATIC/index.html"
cp "$SRC"/*.kicad_sch "$STATIC/kicad/"
# Viewer copy of the board without the fab/assembly drawing annotations.
for pcb in "$SRC"/*.kicad_pcb; do
  python3 "$(dirname "$0")/strip_drawing_layers.py" "$pcb" "$STATIC/kicad/$(basename "$pcb")"
done

# ── Discover outputs ────────────────────────────────────────
RENDERS="[]"
if [ -d "$GEN/3D" ]; then
  BLENDER=$(find "$GEN/3D" -maxdepth 1 -name '*blender*.png' -printf '%f\n' | sort)
  if [ -n "$BLENDER" ]; then
    RENDERS=$(echo "$BLENDER" | jq -R . | jq -s -c .)
  else
    RENDERS=$(find "$GEN/3D" -maxdepth 1 -name '*.png' -printf '%f\n' | sort | jq -R . | jq -s -c .)
  fi
fi

DL="[]"
add() {
  local m
  m=$(set -f; find "$GEN" -name "$2" -type f ${3:-} 2>/dev/null | head -1)  # set -f: keep *ibom* unexpanded
  [ -n "$m" ] && DL=$(echo "$DL" | jq -c --arg n "$1" --arg p "${m#"$GEN"/}" '. + [{"name":$n,"path":$p}]') || true
}
add "Schematic (PDF)" "*schematic*.pdf"
add "BOM (HTML)" "*bom*.html" "! -name *ibom*"
add "BOM (CSV)" "*bom*.csv" "! -name *ibom*"
add "Interactive BOM" "*ibom*.html"
add "Fabrication drawing (PDF)" "*-fabrication.pdf"
add "Assembly drawing (PDF)" "*-assembly.pdf"
add "JLCPCB" "*JLCPCB*.zip"
add "PCBWay" "*PCBWay*.zip"
[ "$TAG" = latest ] && DL="[]"   # downloads are a release-only feature

DIFFS="[]"
for f in $(find "$GEN/Diff" "$GEN/diff" \( -name '*.html' -o -name '*.pdf' \) -type f 2>/dev/null | grep -v '(' | sort -u); do
  NAME=$(basename "${f%.*}" | sed 's/-/ /g; s/\b\(.\)/\u\1/g')
  DIFFS=$(echo "$DIFFS" | jq -c --arg n "$NAME" --arg p "${f#"$GEN"/}" '. + [{"name":$n,"path":$p}]')
done

CHECKS="[]"
for kind in erc drc; do
  f=$(find "$GEN/Checks" -name "*-$kind.html" -type f 2>/dev/null | head -1)
  [ -n "$f" ] && CHECKS=$(echo "$CHECKS" | jq -c --arg n "${kind^^} report" --arg p "${f#"$GEN"/}" '. + [{"name":$n,"path":$p}]')
done

IBOM=$(find "$GEN" -name '*ibom*.html' -type f 2>/dev/null | head -1)
BOM=$(find "$GEN" -name '*bom*.html' -not -name '*ibom*' -type f 2>/dev/null | head -1)

# Root schematic first, then subsheets, then the PCB.
KICAD_FILES=$(cd "$STATIC/kicad" && {
  echo "$SCHEMATIC"
  ls *.kicad_sch | grep -vx "$SCHEMATIC" || true
  ls *.kicad_pcb
} | jq -R . | jq -s -c .)

ASSEMBLY_MODELS="[]"
if [ -f "$GEN/Assembly/assembly-pcb.glb" ]; then
  mkdir -p "$STATIC/assembly"
  cp "$GEN/Assembly/assembly-pcb.glb" "$STATIC/assembly/"
  ASSEMBLY_MODELS='[{"name":"pcb","file":"assembly-pcb.glb"}]'
fi

VERSION=$TAG
[ "$TAG" = latest ] && VERSION=main

{
  echo "baseURL: \"$BASE_URL\""
  echo "params:"
  echo "  version: \"$VERSION\""
  echo "  kicadFiles: $KICAD_FILES"
  echo "  renders: $RENDERS"
  echo "  downloads: $DL"
  echo "  diffs: $DIFFS"
  echo "  checks: $CHECKS"
  [ -n "$IBOM" ] && echo "  ibom: \"${IBOM#"$GEN"/}\""
  [ -n "$BOM" ] && echo "  bom: \"${BOM#"$GEN"/}\""
  [ "$ASSEMBLY_MODELS" != "[]" ] && echo "  assemblyModels: $ASSEMBLY_MODELS"
  true
} > "$SITE/config_override.yaml"

echo "Wrote $SITE/config_override.yaml"
