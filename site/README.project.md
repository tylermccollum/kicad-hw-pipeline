# __PROJECT_TITLE__

Describe the board here.

## Hardware pipeline

Needs Docker (Desktop), git and Python 3.8+ on Windows, macOS or Linux; KiCad,
KiBot and Hugo run in containers. Use `hw` on Windows and `./hw` on
macOS/Linux (or `python site/hw.py` anywhere):

```
hw kibot build host               outputs -> generated/, site -> http://127.0.0.1:8000/
hw serve                          live-reload site at http://localhost:1313/
hw release --tag v1.0.0           strict ERC/DRC, packages in build/releases/
hw help                           all commands
```

- `<project>.kibot.yaml` is the single KiBot config: schematic PDF, HTML BOM,
  iBOM, raytraced renders, schematic/PCB diffs against the previous tag,
  JLCPCB gerbers/drill/BOM/CPL, and the fabrication and assembly drawings.
- Drawing tables and note blocks are CSV templates in `kibot_templates/`;
  `${VAR}` is filled at build time from environment variables, project text
  variables or board statistics (e.g. `${thickness_mm}`, `${pcb_finish_cap}`,
  `${REVISION}`).
- The drawing layout is automatic and never written to your board: each build
  picks the largest standard scale (5:1 down to 1:10) at which the board, its
  dimensions, notes and tables fit the sheet, sizes table text to print ~2 mm
  wide, and places the tables, cover image and outline dimensions, all in a
  throwaway copy. Resize the board or change the sheet (Page Settings) and the
  next build adapts. Anything you draw yourself on the drawing layers is kept.
- To place things by hand, `hw pin-layout` (KiCad closed) copies the current
  layout into the board as `kibot_table_*` / `kibot_image_*` groups and fixes
  the scale; move them in the PCB editor. Delete a group to make it automatic
  again, or `hw unpin-layout` to remove them all.
- `site/` is a Hugo site using the
  [hugo-kicad-site](https://github.com/laenzlinger/hugo-kicad-site) theme
  (GPL-3.0) by laenzlinger, with a vendored
  [KiCanvas](https://kicanvas.org) viewer (MIT; `hw vendor-kicanvas` to
  refresh). Edit the pages in `site/content/`.

### Publishing (GitHub Pages)

`.github/workflows/ci.yml` runs the same `hw` commands: pull requests build
the site as an artifact, pushes to `main` publish `/latest/`, and `v*` tags run
a strict release, attach the packages to a GitHub Release and publish
`/<tag>/`. To enable publishing (public repo): Settings → Pages → deploy from
branch `gh-pages`, then add the Actions variable `PAGES_DEPLOY` = `true`.
