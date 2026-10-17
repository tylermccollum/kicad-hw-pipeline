#!/usr/bin/env bash
# Zip release packages from a KiBot output dir (same set release.yml attaches).
#
#   package.sh <gen_dir> <project> <tag> <out_dir>
set -euo pipefail

GEN=$(cd "${1:?gen_dir}" && pwd)
PROJECT=${2:?project}
TAG=${3:?tag}
OUT=${4:?out_dir}

rm -rf "$OUT"
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

cd "$GEN"
zip -qr "$OUT/$PROJECT-$TAG.zip" . -x '*_pcb3d/*' '*.pcb3d'
cp JLCPCB/*JLCPCB*.zip "$OUT/$PROJECT-jlcpcb-$TAG.zip"
cp JLCPCB/*_bom_jlc.csv JLCPCB/*_cpl_jlc.csv "$OUT/"
cp "Schematic/$PROJECT-schematic.pdf" "BoM/$PROJECT-bom.html" "BoM/$PROJECT-ibom.html" "$OUT/"
cp "Fabrication/$PROJECT-fabrication.pdf" "Assembly/$PROJECT-assembly.pdf" "$OUT/"

echo "Release packages in $OUT:"
ls -1 "$OUT"
