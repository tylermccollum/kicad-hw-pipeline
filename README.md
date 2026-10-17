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
- **A versioned documentation site** (Hugo), hosted locally or on GitHub
  Pages, with Design (written docs and diagrams), Board (interactive 3D
  model and schematic/PCB viewer), Drawings, BOM (iBOM and sourcing),
  Software (firmware/tooling docs placeholder) and Release tabs.
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

   This turns the new folder into a monorepo:

   ```
   my-board/            git root
   ├── README.md        project README with a repository map
   ├── hardware/        the KiCad project (open hardware/my-board.kicad_pro from now on)
   ├── site/            documentation site and build tooling (hw)
   ├── firmware/        placeholder
   └── tools/           placeholder
   ```

   It also initialises git (if needed), writes `.github/workflows/ci.yml`
   (KiCad templates can't carry hidden folders; the source is
   `site/ci/ci.yml`), fills the site title and GitHub URL into
   `site/hugo.yaml` (re-run after adding an `origin` remote), adds the
   empty drawing layers (DrillMap, FabNotes, Dimensions, AsmNotes, AsmCover)
   for your own annotations, and replaces this README.

   For a hardware-only project with everything at the top level, use
   `hw init --flat` instead.
3. Open `hardware/<name>.kicad_pro` in KiCad, replace the placeholder
   50 x 50 mm outline and mounting holes with your design, then build and
   preview:

   ```
   hw kibot build host        (./hw on macOS/Linux)   ->  http://127.0.0.1:8000/
   ```

## Existing repositories

The tooling works in any layout: the KiCad project is found automatically,
or set `site/hw.json` to `{"hardware": "<folder>"}` if a repository has more
than one. `generated/` and `build/` are created inside the KiCad project
folder, so they never collide with a firmware `build/`. After moving
`site/` or the KiCad project, re-run `hw init` so the CI workflow and
launchers follow. CI rebuilds and republishes the site on every push to
`main` (documentation changes included) and builds previews for pull
requests that touch the hardware or the site.

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
