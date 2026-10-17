<!-- hw-pipeline template README: `make -C site init` replaces this file with
     site/README.project.md in projects created from the template. -->
# KiCad hardware pipeline template

A KiCad 10 project template with a containerised manufacturing and
documentation pipeline:

- **KiBot outputs**: schematic PDF, HTML BOM, interactive BOM, raytraced 3D
  renders, schematic/PCB diffs against the previous tag, and a JLCPCB
  package (gerbers, drill, BOM, CPL).
- **Fabrication and assembly drawings** (PDF) with templated notes and data
  tables, laid out and scaled automatically for the board and sheet size.
- **A versioned documentation site** (Hugo) with an embedded schematic/PCB
  viewer, hosted locally or on GitHub Pages.
- **CI**: pull requests build the site, `main` publishes it, `v*` tags make a
  strict release (ERC/DRC must pass) with packages attached.

Everything runs in Docker. The host needs `docker`, `git`, `make`, `rsync`,
`jq`, `zip` and `python3`, and a Linux-like shell (see *Platforms* below).

## Install

Clone this repository into KiCad's user template folder (KiCad skips the
`.git` folder when creating projects):

| OS | KiCad 10 user template folder |
|---|---|
| Linux | `~/.local/share/kicad/10.0/template/` |
| macOS | `~/Documents/KiCad/10.0/template/` |
| Windows | `%USERPROFILE%\Documents\KiCad\10.0\template\` |

(KiCad → Preferences → Configure Paths → `KICAD_USER_TEMPLATE_DIR` shows the
exact location.)

## Create a project

1. In KiCad: **File → New Project from Template → User Templates →
   Hardware pipeline**, choose a folder and name. Every file named
   `hwproject.*` is renamed to your project name.
2. **Close the project in KiCad**, then in the new project folder:

   ```sh
   TITLE="My Board" make -C site init
   ```

   This initialises git (if needed), installs `.github/workflows/ci.yml`
   (KiCad templates can't carry hidden folders, so its source lives in
   `site/ci/`), fills the site title and GitHub URL into `site/hugo.yaml`
   (re-run after adding an `origin` remote), adds the empty drawing layers
   (DrillMap, FabNotes, Dimensions, AsmNotes, AsmCover) for your own
   annotations, and replaces this README with a project README.
3. Replace the placeholder 50 x 50 mm outline and mounting holes with your
   design, then build:

   ```sh
   make -C site kibot build host     # http://127.0.0.1:8000/
   ```

## Using the pipeline

See `README.md` in the created project (from `site/README.project.md`).

## Platforms

- **Linux**: native.
- **Windows**: use WSL2 with Docker Desktop's WSL integration, and keep the
  project inside the WSL filesystem (KiCad for Windows can open it via
  `\\wsl$\...`). Native PowerShell/cmd is not supported.
- **macOS**: needs GNU tools from Homebrew
  (`brew install bash coreutils gnu-sed findutils rsync make`) with their
  `gnubin` directories first in `PATH`. On Apple Silicon the KiCad container
  runs under x86-64 emulation, which is slower.

## Maintaining this template

This template is exported from a working project with
`make -C site export-template DEST=<this folder>`; the export keeps `.git`
and replaces everything else, so make changes in the source project and
re-export, then commit here.
