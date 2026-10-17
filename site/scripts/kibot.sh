#!/usr/bin/env bash
# Run KiBot in the KiCad 10 container against a scratch clone of the repo.
#
#   kibot.sh <ref> <out_dir>
#
# <ref> is a tag/commit, or WORKTREE for the current checkout including
# uncommitted changes. KiBot edits the project in place (text variables, zone
# refills), so it never runs on your real files.
#
# Env: STRICT=1 makes ERC/DRC violations fatal (releases).
#      KIBOT_IMAGE, PROJECT, KICAD_DIR override the defaults below
#      (PROJECT defaults to the single *.kicad_pro in KICAD_DIR).
set -euo pipefail

REF=${1:?usage: kibot.sh <ref|WORKTREE> <out_dir>}
OUT=${2:?usage: kibot.sh <ref|WORKTREE> <out_dir>}
REPO=$(git rev-parse --show-toplevel)
KICAD_DIR=${KICAD_DIR:-.}
if [ -z "${PROJECT:-}" ]; then
  pros=("$REPO/$KICAD_DIR"/*.kicad_pro)
  [ ${#pros[@]} -eq 1 ] && [ -f "${pros[0]}" ] || { echo "ERROR: expected one .kicad_pro in $REPO/$KICAD_DIR; set PROJECT" >&2; exit 1; }
  PROJECT=$(basename "${pros[0]}" .kicad_pro)
fi
KIBOT_IMAGE=${KIBOT_IMAGE:-ghcr.io/inti-cmnb/kicad10_auto_full@sha256:7b720dc99b2775e4f4133f06887cf9a53c85a8403438d6684ecf0916d124f52a}
STRICT=${STRICT:-0}

WORK="$REPO/build/work/${REF//\//_}"
CACHE="$REPO/build/cache_3d"

# Fresh local clone: has every tag (diffs, git describe) and is cheap (hardlinks).
rm -rf "$WORK"
mkdir -p "$(dirname "$WORK")" "$CACHE"
if [ "$REF" = WORKTREE ] && ! git -C "$REPO" rev-parse -q --verify HEAD >/dev/null; then
  # Brand-new repo without commits: snapshot the working tree into a scratch repo.
  mkdir -p "$WORK"
  git -C "$REPO" ls-files -z -co --exclude-standard | rsync -a --from0 --files-from=- "$REPO/" "$WORK/"
  git -C "$WORK" init -q
  git -C "$WORK" add -A
  git -C "$WORK" -c user.name=kibot -c user.email=kibot@localhost commit -q -m snapshot
elif [ "$REF" = WORKTREE ]; then
  git clone -q --local --no-checkout "$REPO" "$WORK"
  git -C "$WORK" checkout -q --detach "$(git -C "$REPO" rev-parse HEAD)"
  # Overlay tracked + untracked (non-ignored) files from the working tree.
  git -C "$REPO" ls-files -z -co --exclude-standard \
    | rsync -a --from0 --ignore-missing-args --files-from=- "$REPO/" "$WORK/"
else
  git clone -q --local --no-checkout "$REPO" "$WORK"
  git -C "$WORK" checkout -q --detach "$REF"
  # Tags made before the pipeline existed have no KiBot config: borrow the current one.
  CFG="$KICAD_DIR/$PROJECT.kibot.yaml"
  if [ ! -f "$WORK/$CFG" ]; then
    echo "NOTE: $REF has no $CFG; using the working tree's copy"
    cp "$REPO/$CFG" "$WORK/$CFG"
  fi
fi

DEFS="-E CHECKS_DONT_STOP=$([ "$STRICT" = 1 ] && echo false || echo true)"
# KIBOT_TAG-0 is HEAD's own tag on a tagged commit; skip it to diff vs the previous release.
git -C "$WORK" describe --exact-match --tags HEAD >/dev/null 2>&1 && DEFS="$DEFS -E DIFF_SKIP_TAGS=1"
ARGS="-c $PROJECT.kibot.yaml -e $PROJECT.kicad_sch -b $PROJECT.kicad_pcb -d /out"

rm -rf "$OUT"
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

# Drawing layout (layers, scale, table/image placeholders, outline dimensions)
# is generated here, in the throwaway copy only; pinned placeholders on the
# real board are kept as placed. Tags older than the pipeline also need the
# drawing templates.
[ -d "$WORK/$KICAD_DIR/kibot_templates" ] || cp -r "$REPO/kibot_templates" "$WORK/$KICAD_DIR/"
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$WORK:/w" -v "$REPO/site/scripts:/scripts:ro" \
  -w "/w/$KICAD_DIR" "$KIBOT_IMAGE" python3 /scripts/setup_drawing_layers.py \
  "$PROJECT.kicad_pcb" "$PROJECT.kibot.yaml" 2>&1 | grep -v 'assert'

docker run --rm \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -e KICAD10_3DMODEL_DIR=/usr/share/kicad/3dmodels -e KIBOT_3D_MODELS=/cache_3d \
  -v "$WORK:/w" -v "$OUT:/out" -v "$CACHE:/cache_3d" \
  -w "/w/$KICAD_DIR" "$KIBOT_IMAGE" bash -c "
    set -e
    git config --global --add safe.directory '*'
    kibot $ARGS $DEFS
    # Diffs are best-effort (e.g. no earlier tag to compare against).
    for t in diff_sch diff_pcb; do
      kibot $ARGS $DEFS -s run_erc,run_drc \$t || echo \"WARNING: \$t failed (no previous tag?)\"
    done"

echo "KiBot outputs: $OUT"
