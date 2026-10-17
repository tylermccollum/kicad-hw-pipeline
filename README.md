<!-- hw-pipeline template README: `hw init` replaces this file with
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

## Requirements

Works the same on **Windows, macOS and Linux**. You need:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Linux:
  Docker Engine), running. KiCad, KiBot and Hugo all run in containers.
- [git](https://git-scm.com/downloads)
- [Python 3.8+](https://www.python.org/downloads/) (Windows: tick "Add
  python.exe to PATH" in the installer, or use the `py` launcher it installs)

No WSL, make, bash or other Unix tools are needed. On Apple Silicon Macs the
KiCad container runs under x86-64 emulation, so builds are slower.

## Install

Clone this repository into KiCad's user template folder (KiCad skips the
`.git` folder when creating projects):

| OS | KiCad 10 user template folder |
|---|---|
| Windows | `%USERPROFILE%\Documents\KiCad\10.0\template\` |
| macOS | `~/Documents/KiCad/10.0/template/` |
| Linux | `~/.local/share/kicad/10.0/template/` |

(KiCad → Preferences → Configure Paths → `KICAD_USER_TEMPLATE_DIR` shows the
exact location.)

## Create a project

1. In KiCad: **File → New Project from Template → User Templates →
   Hardware pipeline**, choose a folder and name. Every file named
   `hwproject.*` is renamed to your project name.
2. **Close the project in KiCad**, then open a terminal in the new project
   folder and run:

   | Windows (cmd or PowerShell) | macOS / Linux |
   |---|---|
   | `hw init --title "My Board"` | `./hw init --title "My Board"` |

   This initialises git (if needed), installs `.github/workflows/ci.yml`
   (KiCad templates can't carry hidden folders, so its source lives in
   `site/ci/`), fills the site title and GitHub URL into `site/hugo.yaml`
   (re-run after adding an `origin` remote), adds the empty drawing layers
   (DrillMap, FabNotes, Dimensions, AsmNotes, AsmCover) for your own
   annotations, and replaces this README with a project README.
3. Replace the placeholder 50 x 50 mm outline and mounting holes with your
   design, then build and preview:

   ```
   hw kibot build host        (./hw on macOS/Linux)   ->  http://127.0.0.1:8000/
   ```

If `hw` isn't found, run the tool directly: `python site/hw.py ...`
(`py site\hw.py ...` on Windows, `python3 site/hw.py ...` on macOS/Linux).

## Using the pipeline

See `README.md` in the created project (from `site/README.project.md`), or
`hw help`.

## Maintaining this template

This template is exported from a working project with
`hw export-template --dest <this folder>`; the export keeps `.git` and
replaces everything else, so make changes in the source project, re-export,
then commit here.
