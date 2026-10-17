#!/usr/bin/env python3
"""hw: build, preview and release this KiCad project (Linux, macOS, Windows).

    ./hw <command> [<command> ...] [options]        (Linux/macOS)
    hw <command> [<command> ...] [options]          (Windows: hw.cmd)
    python3 site/hw.py <command> ...                (anywhere)

Works for a flat project (KiCad files, site/ and hw at the repo root) and for
a monorepo (e.g. hardware/, site/, firmware/, tools/): the KiCad project is
found automatically (or set site/hw.json {"hardware": "<folder>"}).
generated/ and build/ are created in the KiCad project's folder.

Commands run in the order given, e.g. `hw kibot build host`.

  init             one-time setup after creating a project from the template
                   (moves it into a hardware/ + site/ + firmware/ + tools/ monorepo
                   unless --flat)
  kibot            run KiBot on the working tree (incl. uncommitted changes) -> generated/
  build            build the site for the working tree -> build/www/latest
  serve            live-reload site at http://localhost:1313/
  host             serve every built version at http://127.0.0.1:8000/
  release          --tag vX.Y.Z: strict ERC/DRC, packages in build/releases/, versioned site
  pin-layout       copy the drawing layout into the board to adjust by hand (KiCad closed)
  unpin-layout     remove pinned drawing placeholders; back to automatic layout
  vendor           refresh the vendored viewers (KiCanvas, Online3DViewer, Mermaid)
  export-template  write this tooling as a KiCad project template (--dest)
  clean            remove local build state (keeps build/releases)
  versions         regenerate versions.json/index.html in a site folder (--www)

Host requirements: Docker (Desktop), git and Python 3.8+. KiCad, KiBot and
Hugo all run in containers.
"""
import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from functools import partial
from pathlib import Path

SITE = Path(__file__).resolve().parent
SCRIPTS = SITE / "scripts"
# KiCad 10 + KiBot, pinned by digest so release outputs are reproducible.
KIBOT_IMAGE = os.environ.get(
    "KIBOT_IMAGE",
    "ghcr.io/inti-cmnb/kicad10_auto_full@sha256:7b720dc99b2775e4f4133f06887cf9a53c85a8403438d6684ecf0916d124f52a")
HUGO_IMAGE = os.environ.get("HUGO_IMAGE", "hugomods/hugo:std-go-git-0.160.1")
TEMPLATE_BASE = "hwproject"   # project name inside the exported KiCad template
STAGED_DIRS = ("3D", "Assembly", "BoM", "Checks", "Diff", "Fabrication", "JLCPCB", "Schematic",
               "kicad", "assembly")

# Windows consoles may not be UTF-8: never crash on unusual characters in tool output.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")
sys.path.insert(0, str(SCRIPTS))
from strip_drawing_layers import strip  # noqa: E402


class HwError(Exception):
    pass


# ── Repository layout ────────────────────────────────────────────────────────
# Three independent locations, so the same tooling works for a flat project
# (KiCad files, site/ and hw at the repo root) and a monorepo (e.g. hardware/,
# site/, firmware/, tools/ under one git root):
#   ROOT  git repository root (CI, .github/, tags); before `git init`, the
#         folder above site/
#   HW    folder with the single .kicad_pro, its KiBot config and
#         kibot_templates/; generated/ and build/ go here too
#   SITE  folder with this script
# HW is found next to site/ first, then by searching the repo's non-ignored
# files; override with site/hw.json {"hardware": "<path from ROOT>"} or HW_DIR.
SKIP_DIRS = {".git", "build", "generated", "node_modules", "__pycache__"}


def _git_root(start):
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def _find_hardware(root):
    override = os.environ.get("HW_DIR")
    cfg = SITE / "hw.json"
    if not override and cfg.exists():
        override = json.loads(cfg.read_text(encoding="utf-8")).get("hardware")
    if override:
        hw = (root / override).resolve()
        if not list(hw.glob("*.kicad_pro")):
            raise HwError(f"no .kicad_pro in {hw} (from {'HW_DIR' if os.environ.get('HW_DIR') else cfg})")
        return hw
    if len(list(SITE.parent.glob("*.kicad_pro"))) == 1:    # flat layout: next to site/
        return SITE.parent
    if (root / ".git").exists():
        r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", "*.kicad_pro"],
                           cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        pros = [root / f for f in r.stdout.splitlines() if f]
    else:
        pros = [p for p in root.rglob("*.kicad_pro") if not SKIP_DIRS & set(p.relative_to(root).parts)]
    pros = [p for p in pros if not any(part.endswith("-backups") or part in SKIP_DIRS
                                       for part in p.relative_to(root).parts[:-1])]
    dirs = sorted({p.parent.resolve() for p in pros})
    if len(dirs) != 1:
        found = ", ".join(str(d.relative_to(root)) or "." for d in dirs) or "none"
        raise HwError(f"can't tell which KiCad project to build (found: {found}); "
                      f'create {cfg.relative_to(root)} with {{"hardware": "<folder>"}}')
    return dirs[0]


class Layout:
    def __init__(self):
        self.root = _git_root(SITE) or SITE.parent
        self.hw = _find_hardware(self.root)
        self.build = self.hw / "build"
        self.gen = self.hw / "generated"
        self.www = self.build / "www"
        self.releases = self.build / "releases"

    def rel(self, path):
        """Path relative to ROOT, POSIX style ('.' for ROOT itself)."""
        return Path(path).resolve().relative_to(self.root).as_posix()

    @property
    def hw_rel(self):
        return self.rel(self.hw)


_layout = None


def L():
    global _layout
    if _layout is None:
        _layout = Layout()
    return _layout


# ── Helpers ──────────────────────────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def rmtree(path):
    """shutil.rmtree that also removes read-only files (git objects on Windows)."""
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if Path(path).exists():
        shutil.rmtree(path, onerror=onerror)


def run(cmd, cwd=None, check=True, capture=False, drop=None):
    """Run a command. `drop`: substring of output lines to hide (pcbnew noise)."""
    if capture:
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and r.returncode:
            raise HwError(f"{' '.join(map(str, cmd))} failed:\n{r.stderr.strip()}")
        return r.stdout.strip()
    if drop is None:
        r = subprocess.run(cmd, cwd=cwd)
        code = r.returncode
    else:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace")
        for line in p.stdout:
            if drop not in line:
                print(line, end="", flush=True)
        code = p.wait()
    if check and code:
        raise HwError(f"command failed ({code}): {' '.join(map(str, cmd))}")
    return code


def git(*args, cwd=None, check=True):
    return run(["git", *args], cwd=cwd or L().root, check=check, capture=True)


def has_commits(repo):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=repo,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def project_name(root=None):
    root = root or L().hw
    if os.environ.get("PROJECT"):
        return os.environ["PROJECT"]
    pros = sorted(Path(root).glob("*.kicad_pro"))
    if len(pros) != 1:
        raise HwError(f"expected exactly one .kicad_pro in {root} (found {len(pros)}); set PROJECT=<name>")
    return pros[0].stem


def board_is_open(project):
    return (L().hw / f"~{project}.kicad_pcb.lck").exists()


def docker(image, cmd, mounts=(), workdir=None, env=None, ports=(), drop=None, label="container"):
    """docker run --rm, portable: bind mounts via --mount (Windows paths have ':'),
    and on Linux run as the calling user so outputs aren't owned by root."""
    args = ["docker", "run", "--rm"]
    if image == KIBOT_IMAGE:
        args += ["--platform", "linux/amd64"]   # x86-64 only; emulated on Apple Silicon
    if sys.platform.startswith("linux"):
        args += ["-u", f"{os.getuid()}:{os.getgid()}"]
    args += ["-e", "HOME=/tmp"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    for host, target, *ro in mounts:
        Path(host).mkdir(parents=True, exist_ok=True)
        spec = f"type=bind,source={Path(host).resolve()},target={target}"
        args += ["--mount", spec + (",readonly" if ro and ro[0] else "")]
    for p in ports:
        args += ["-p", p]
    if workdir:
        args += ["-w", workdir]
    code = run(args + [image] + list(cmd), drop=drop, check=False)
    if code:
        raise HwError(f"{label} failed (exit {code}); see the output above")
    return code


def copy_worktree(dst):
    """Copy the hardware folder's tracked + untracked (non-ignored) files from the
    working tree to dst (paths relative to ROOT)."""
    files = git("ls-files", "-z", "-co", "--exclude-standard", "--", L().hw_rel).split("\0")
    for f in filter(None, files):
        src = L().root / f
        if src.is_file():
            out = Path(dst) / f
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)


def kibot_defs(text):
    m = re.search(r"^  DRAWING_SCALE: *(.+?) *$", text, re.M)
    return m.group(1) if m else None


# ── KiBot ────────────────────────────────────────────────────────────────────
def kibot(ref, out, strict=False):
    """Run KiBot in the KiCad container against a throwaway copy of the repo.

    ref is a tag/commit, or WORKTREE for the current checkout incl. uncommitted
    changes. KiBot edits the project in place (text variables, zone refills) and
    the drawing layout is generated in the copy, so real files are never touched.
    """
    lay = L()
    project = project_name()
    work = lay.build / "work" / ref.replace("/", "_")   # copy of the whole repo (ROOT)
    whw = work / lay.hw_rel                             # hardware folder inside the copy
    cache = lay.build / "cache_3d"
    rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    if ref == "WORKTREE" and not has_commits(lay.root):
        # Brand-new repo without commits: snapshot the working tree into a scratch repo.
        work.mkdir()
        copy_worktree(work)
        git("init", "-q", cwd=work)
        git("add", "-A", cwd=work)
        git("-c", "user.name=kibot", "-c", "user.email=kibot@localhost", "commit", "-q", "-m", "snapshot",
            cwd=work)
    elif ref == "WORKTREE":
        # Local clone: has every tag (diffs, git describe) and is cheap.
        git("clone", "-q", "--local", "--no-checkout", str(lay.root), str(work))
        git("checkout", "-q", "--detach", git("rev-parse", "HEAD"), cwd=work)
        copy_worktree(work)
    else:
        git("clone", "-q", "--local", "--no-checkout", str(lay.root), str(work))
        git("checkout", "-q", "--detach", ref, cwd=work)
        cfg = f"{project}.kibot.yaml"
        if not (whw / cfg).exists():   # tags made before the pipeline existed
            log(f"NOTE: {ref} has no {cfg}; using the working tree's copy")
            whw.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lay.hw / cfg, whw / cfg)
    if not (whw / "kibot_templates").exists():
        shutil.copytree(lay.hw / "kibot_templates", whw / "kibot_templates")

    defs = ["-E", f"CHECKS_DONT_STOP={'false' if strict else 'true'}"]
    tagged = subprocess.run(["git", "describe", "--exact-match", "--tags", "HEAD"], cwd=work,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if tagged:   # KIBOT_TAG-0 would be HEAD's own tag: diff against the previous release
        defs += ["-E", "DIFF_SKIP_TAGS=1"]
    rmtree(out)
    Path(out).mkdir(parents=True)

    # Drawing layout: layers, scale, table/image placeholders, outline dimensions.
    docker(KIBOT_IMAGE, ["python3", "/scripts/setup_drawing_layers.py",
                         f"{project}.kicad_pcb", f"{project}.kibot.yaml"],
           mounts=[(work, "/w"), (SCRIPTS, "/scripts", True)], workdir=f"/w/{lay.hw_rel}", drop="assert",
           label="drawing layout")

    base = f"-c {project}.kibot.yaml -e {project}.kicad_sch -b {project}.kicad_pcb -d /out {' '.join(defs)}"
    script = (f"set -e; git config --global --add safe.directory '*'; kibot {base}; "
              # Diffs are best-effort (e.g. no earlier tag to compare against).
              f"for t in diff_sch diff_pcb; do kibot {base} -s run_erc,run_drc $t "
              f"|| echo \"WARNING: $t failed (no previous tag?)\"; done")
    docker(KIBOT_IMAGE, ["bash", "-c", script],
           mounts=[(work, "/w"), (out, "/out"), (cache, "/cache_3d")], workdir=f"/w/{lay.hw_rel}",
           env={"KICAD10_3DMODEL_DIR": "/usr/share/kicad/3dmodels", "KIBOT_3D_MODELS": "/cache_3d"},
           label="KiBot" + (" (strict: ERC/DRC errors fail a release)" if strict else ""))
    log(f"KiBot outputs: {out}")


# ── Site ─────────────────────────────────────────────────────────────────────
def first(paths, pattern, exclude=None):
    for p in paths:
        if Path(p).match(pattern) and not (exclude and Path(p).match(exclude)):
            return p
    return None


def prepare(gen, src, tag, base_url):
    """Stage KiBot outputs into site/static and write site/config_override.yaml
    (mirrors the discovery steps of laenzlinger/hugo-kicad-site's workflow)."""
    gen, src, static = Path(gen), Path(src), SITE / "static"
    schematic = f"{project_name(src)}.kicad_sch"

    # Only clear what a previous run staged; hand-added static files stay.
    for name in {p.name for p in gen.iterdir()} | {"kicad", "assembly"}:
        target = static / name
        if target.is_dir():
            rmtree(target)
        elif target.exists():
            target.unlink()
    shutil.copytree(gen, static, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*_pcb3d", "*.pcb3d"))
    (static / "index.html").unlink(missing_ok=True)
    kicad = static / "kicad"
    kicad.mkdir(parents=True, exist_ok=True)
    for sch in src.glob("*.kicad_sch"):
        shutil.copy2(sch, kicad)
    for pcb in src.glob("*.kicad_pcb"):   # viewer copy without the drawing annotations
        strip(pcb, kicad / pcb.name)

    files = sorted(p for p in gen.rglob("*") if p.is_file() and "_pcb3d" not in str(p))
    rel = lambda p: p.relative_to(gen).as_posix()  # noqa: E731

    renders = []
    if (gen / "3D").is_dir():
        pngs = sorted(p.name for p in (gen / "3D").glob("*.png"))
        renders = [n for n in pngs if "blender" in n] or pngs

    downloads = []
    for name, pattern, exclude in [("Schematic (PDF)", "*schematic*.pdf", None),
                                   ("BOM (HTML)", "*bom*.html", "*ibom*"),
                                   ("BOM (CSV)", "*bom*.csv", "*ibom*"),
                                   ("Interactive BOM", "*ibom*.html", None),
                                   ("Fabrication drawing (PDF)", "*-fabrication.pdf", None),
                                   ("Assembly drawing (PDF)", "*-assembly.pdf", None),
                                   ("JLCPCB", "*JLCPCB*.zip", None),
                                   ("PCBWay", "*PCBWay*.zip", None)]:
        hit = first(files, pattern, exclude)
        if hit:
            downloads.append({"name": name, "path": rel(hit)})
    if tag == "latest":
        downloads = []   # downloads are a release-only feature

    diffs = []
    for p in files:
        if p.parent.name in ("Diff", "diff") and p.suffix in (".html", ".pdf") and "(" not in p.name:
            title = re.sub(r"\b(.)", lambda m: m.group(1).upper(), p.stem.replace("-", " "))
            diffs.append({"name": title, "path": rel(p)})

    checks = []
    for kind in ("erc", "drc"):
        hit = first([p for p in files if p.parent.name == "Checks"], f"*-{kind}.html")
        if hit:
            checks.append({"name": f"{kind.upper()} report", "path": rel(hit)})

    ibom = first(files, "*ibom*.html")
    bom = first(files, "*bom*.html", "*ibom*")
    sheets = sorted(p.name for p in kicad.glob("*.kicad_sch"))
    kicad_files = ([schematic] if schematic in sheets else []) + [s for s in sheets if s != schematic] \
        + sorted(p.name for p in kicad.glob("*.kicad_pcb"))

    assembly = []
    glb = gen / "Assembly" / "assembly-pcb.glb"
    if glb.exists():
        (static / "assembly").mkdir(exist_ok=True)
        shutil.copy2(glb, static / "assembly")
        if renders:   # preview image shown until the 3D model is loaded
            shutil.copy2(gen / "3D" / renders[0], static / "assembly" / "assembly-pcb.png")
        assembly = [{"name": "pcb", "file": glb.name}]

    def found(spec):
        out = []
        for name, pattern, exclude in spec:
            hit = first(files, pattern, exclude)
            if hit:
                out.append({"name": name, "path": rel(hit)})
        return out
    # Drawings and BOM files are shown for every build (downloads: releases only)
    drawings = found([("Fabrication drawing", "*-fabrication.pdf", None),
                      ("Assembly drawing", "*-assembly.pdf", None),
                      ("Schematic", "*schematic*.pdf", None)])
    bom_files = found([("Interactive BOM (HTML)", "*ibom*.html", None),
                       ("BOM (HTML)", "*-bom.html", "*ibom*"),
                       ("BOM (CSV)", "BoM/*-bom.csv", None),
                       ("JLCPCB BOM (CSV)", "*_bom_jlc.csv", None),
                       ("JLCPCB pick and place (CSV)", "*_cpl_jlc.csv", None)])
    bom_csv = first(files, "BoM/*-bom.csv")

    j = partial(json.dumps, ensure_ascii=False)   # JSON is valid YAML flow syntax
    lines = [f"baseURL: {j(base_url)}", "params:",
             f"  version: {j('main' if tag == 'latest' else tag)}",
             f"  kicadFiles: {j(kicad_files)}", f"  renders: {j(renders)}",
             f"  downloads: {j(downloads)}", f"  diffs: {j(diffs)}", f"  checks: {j(checks)}"]
    if ibom:
        lines.append(f"  ibom: {j(rel(ibom))}")
    if bom:
        lines.append(f"  bom: {j(rel(bom))}")
    if assembly:
        lines.append(f"  assemblyModels: {j(assembly)}")
    lines.append(f"  drawings: {j(drawings)}")
    lines.append(f"  bomFiles: {j(bom_files)}")
    if bom_csv:
        lines.append(f"  bomCsv: {j(rel(bom_csv))}")
    (SITE / "config_override.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {SITE / 'config_override.yaml'}")


def hugo(shell_cmd, ports=()):
    L().www.mkdir(parents=True, exist_ok=True)
    docker(HUGO_IMAGE, ["sh", "-c", f"hugo mod get && {shell_cmd}"],
           mounts=[(SITE, "/src"), (L().build / "hugo_cache", "/cache"), (L().www, "/www")],
           workdir="/src", env={"HUGO_CACHEDIR": "/cache"}, ports=ports, label="Hugo")


def versions(www=None):
    """versions.json + redirecting index.html at the hosting root (gh-pages layout)."""
    www = Path(www) if www else L().www

    def key(tag):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", tag)]
    tags = (["latest"] if (www / "latest").is_dir() else []) + sorted(
        (d.name for d in www.iterdir() if d.is_dir() and d.name.startswith("v")), key=key, reverse=True)
    entries = [{"tag": t} for t in tags]
    if entries:
        entries[0]["latest"] = True
    (www / "versions.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    (www / "index.html").write_text('<meta http-equiv="refresh" content="0;url=latest/">\n', encoding="utf-8")
    (www / ".nojekyll").touch()   # GitHub Pages: serve files as-is
    log("Versions: " + ", ".join(tags))


def build(base_path):
    prepare(L().gen, L().hw, "latest", f"{base_path}/latest/")
    hugo("hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/latest")
    versions()


def serve(base_path, host_addr):
    prepare(L().gen, L().hw, "latest", f"{base_path}/latest/")
    hugo("hugo server -D --bind 0.0.0.0 --baseURL http://localhost:1313/ "
         "--config hugo.yaml,config_override.yaml", ports=[f"{host_addr}:1313:1313"])


def host(host_addr, port):
    www = L().www
    www.mkdir(parents=True, exist_ok=True)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(www))
    try:
        httpd = http.server.ThreadingHTTPServer((host_addr, port), handler)
    except OSError as e:
        raise HwError(f"can't serve on {host_addr}:{port} ({e.strerror}); is another `hw host` "
                      f"already running? Stop it, or use --port <n>")
    log(f"Serving {www} at http://{host_addr}:{port}/  (Ctrl-C to stop)")
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def package(gen, project, tag, out):
    """Release packages: everything zipped, plus the fab/assembly files on their own."""
    gen, out = Path(gen), Path(out)
    rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(out / f"{project}-{tag}.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(gen.rglob("*")):
            if p.is_file() and "_pcb3d" not in p.as_posix() and p.suffix != ".pcb3d":
                z.write(p, p.relative_to(gen).as_posix())
    jlc = sorted((gen / "JLCPCB").glob("*JLCPCB*.zip"))
    if jlc:
        shutil.copy2(jlc[0], out / f"{project}-jlcpcb-{tag}.zip")
    for p in [*sorted((gen / "JLCPCB").glob("*_bom_jlc.csv")), *sorted((gen / "JLCPCB").glob("*_cpl_jlc.csv")),
              gen / "Schematic" / f"{project}-schematic.pdf", gen / "BoM" / f"{project}-bom.html",
              gen / "BoM" / f"{project}-ibom.html", gen / "Fabrication" / f"{project}-fabrication.pdf",
              gen / "Assembly" / f"{project}-assembly.pdf"]:
        if p.exists():
            shutil.copy2(p, out)
        else:
            log(f"WARNING: missing {p.relative_to(gen)}")
    log(f"Release packages in {out}:")
    for p in sorted(out.iterdir()):
        log(f"  {p.name}")


def release(tag, strict, base_path):
    if not tag:
        raise HwError("release needs --tag vX.Y.Z")
    lay = L()
    gen = lay.build / "tags" / tag / "generated"
    kibot(tag, gen, strict=strict)
    package(gen, project_name(), tag, lay.releases / tag)
    prepare(gen, lay.build / "work" / tag.replace("/", "_") / lay.hw_rel, tag, f"{base_path}/{tag}/")
    hugo(f"hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/{tag}")
    versions()


# ── Board layout (real board: init / pin / unpin) ───────────────────────────
def setup_layout(mode):
    project = project_name()
    if board_is_open(project):
        raise HwError("close the board in KiCad first")
    docker(KIBOT_IMAGE, ["python3", "/scripts/setup_drawing_layers.py",
                         f"{project}.kicad_pcb", f"{project}.kibot.yaml", mode],
           mounts=[(L().hw, "/w"), (SCRIPTS, "/scripts", True)], workdir="/w", drop="assert",
           label="board layout")


def launcher_sh(site_rel):
    return ("#!/bin/sh\n# hw: project tooling (Linux/macOS). See `./hw help`. Windows: hw.cmd\n"
            f'exec python3 "$(dirname "$0")/{site_rel}/hw.py" "$@"\n')


def launcher_cmd(site_rel):
    win = site_rel.replace("/", "\\")
    return "\r\n".join([
        "@echo off",
        "rem hw: project tooling (Windows cmd/PowerShell). See `hw help`.",
        "rem Uses the py launcher from python.org installers, else python on PATH.",
        "where py >nul 2>nul",
        "if %errorlevel%==0 (",
        f'  py -3 "%~dp0{win}\\hw.py" %*',
        ") else (",
        f'  python "%~dp0{win}\\hw.py" %*',
        ")",
        "exit /b %errorlevel%", ""])


def render_ci():
    """site/ci/ci.yml with this repository's layout filled in."""
    lay = L()
    hwp = "" if lay.hw_rel == "." else lay.hw_rel + "/"
    text = (SITE / "ci" / "ci.yml").read_text(encoding="utf-8")
    if hwp:   # monorepo: any change in the hardware folder (libraries, subfolders) triggers CI
        text = text.replace('"__HWP__*.kicad_*", "__HWP__*.kibot.yaml", "__HWP__*.kicad_sym", '
                            '"__HWP__kibot_templates/**"', '"__HWP__**"')
    return text.replace("__SITE__", lay.rel(SITE)).replace("__OUT__", lay.rel(lay.build)).replace("__HWP__", hwp)


def write_if_changed(path, content, label, newline=None):
    path = Path(path)
    if path.exists() and path.read_bytes() == content.encode("utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline or "") as f:
        f.write(content)
    log(f"+ {label}")
    return True


TEMPLATE_MARKER = "hw-pipeline template README"
KEEP_AT_ROOT = {".git", ".github", ".gitignore", ".gitattributes", "README.md", "hw", "hw.cmd",
                "site", "hardware", "firmware", "tools", "build", "generated"}
PLACEHOLDER_README = {
    "firmware": "# Firmware\n\nFirmware for the board's MCU. Document it on the site's Software tab\n"
                "(`site/content/software/`).\n",
    "tools": "# Tools\n\nHost-side tooling: flashing, calibration, test scripts. Document it on the\n"
             "site's Software tab (`site/content/software/`).\n",
}


def to_monorepo():
    """Reorganise a project fresh from the (flat) KiCad template into
        README.md  hardware/  site/  firmware/  tools/
    KiCad's New-from-Template always opens <folder>/<name>.kicad_pro, so the
    template itself has to stay flat; this runs once from `hw init`."""
    root = SITE.parent
    locks = sorted(p.name for p in root.glob("~*.lck"))
    if locks:
        raise HwError(f"close the project in KiCad first ({', '.join(locks)}); its files are about to move")
    hardware = root / "hardware"
    hardware.mkdir(exist_ok=True)
    for p in sorted(root.iterdir()):
        if p.name not in KEEP_AT_ROOT:
            shutil.move(str(p), str(hardware / p.name))
    log("+ hardware/ (KiCad project moved here; open hardware/*.kicad_pro in KiCad from now on)")
    for folder, text in PLACEHOLDER_README.items():
        (root / folder).mkdir(exist_ok=True)
        if not (root / folder / "README.md").exists():
            (root / folder / "README.md").write_text(text, encoding="utf-8")
            log(f"+ {folder}/README.md")


def init(title, flat=False):
    """One-time setup for a project created from the template. Safe to re-run.
    A fresh template project is reorganised into a monorepo (hardware/, site/,
    firmware/, tools/) unless `flat`; existing projects are never moved."""
    global _layout
    root_readme = SITE.parent / "README.md"
    fresh = root_readme.exists() and TEMPLATE_MARKER in root_readme.read_text(encoding="utf-8")
    monorepo = fresh and not flat and list(SITE.parent.glob("*.kicad_pro"))
    if monorepo:
        to_monorepo()
        _layout = None    # the KiCad project moved: rediscover the layout
    lay = L()
    project = project_name()
    if not _git_root(SITE):
        git("init", "-q", cwd=lay.root)
        log(f"+ git init ({lay.root})")
    site_rel = lay.rel(SITE)

    # CI workflow at the git root (the only place GitHub reads it), for this layout
    write_if_changed(lay.root / ".github" / "workflows" / "ci.yml", render_ci(), ".github/workflows/ci.yml")

    # Launchers at the git root, pointing at site/ wherever it is
    if write_if_changed(lay.root / "hw", launcher_sh(site_rel), "hw (launcher)"):
        os.chmod(lay.root / "hw", 0o755)
    write_if_changed(lay.root / "hw.cmd", launcher_cmd(site_rel), "hw.cmd (launcher)")
    if lay.hw != lay.root and not (lay.hw / "site" / "hw.py").exists():
        for stale in ("hw", "hw.cmd"):   # the template's launchers, if site/ was moved out
            if (lay.hw / stale).exists():
                (lay.hw / stale).unlink()
                log(f"- {lay.rel(lay.hw / stale)} (site/ is elsewhere; use the root launcher)")
    if not (lay.root / ".gitattributes").exists() and (lay.hw / ".gitattributes").exists():
        shutil.copy2(lay.hw / ".gitattributes", lay.root / ".gitattributes")
        log("+ .gitattributes")

    # Ignore build output and staged site assets, relative to the git root
    ignore = lay.root / ".gitignore"
    existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    if lay.hw != lay.root and (lay.hw / ".gitignore").exists():   # the template's own, in hardware/
        existing += [f"{lay.hw_rel}/{x.strip('/')}/" for x in ("generated", "build")
                     if f"{x}/" in (lay.hw / ".gitignore").read_text(encoding="utf-8").splitlines()]
    wanted = [f"{lay.rel(lay.gen)}/", f"{lay.rel(lay.build)}/"] + \
             [f"{site_rel}/{x}" for x in ("public/", "resources/", "config_override.yaml", ".hugo_build.lock")] + \
             [f"{site_rel}/static/{d}/" for d in STAGED_DIRS]
    # a bare `name/` pattern (no other slash) already matches that folder at any depth
    anywhere = {e.strip().strip("/") for e in existing if e.strip().endswith("/") and "/" not in e.strip().strip("/")}
    missing = [w for w in wanted if w not in existing and w.rstrip("/").split("/")[-1] not in anywhere]
    if missing:
        with open(ignore, "a", encoding="utf-8", newline="\n") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("# hw pipeline: build output and staged site assets\n" + "\n".join(missing) + "\n")
        log(f"+ .gitignore ({len(missing)} entries)")

    readme, project_readme = lay.root / "README.md", SITE / "README.project.md"
    if readme.exists() and TEMPLATE_MARKER in readme.read_text(encoding="utf-8") and project_readme.exists():
        text = project_readme.read_text(encoding="utf-8")
        if lay.hw == lay.root:   # flat: drop the repository map
            text = re.sub(r"<!-- monorepo -->.*?<!-- /monorepo -->\n*", "", text, flags=re.S)
        else:
            text = text.replace("<!-- monorepo -->\n", "").replace("\n<!-- /monorepo -->\n", "")
        readme.write_text(text, encoding="utf-8")
        project_readme.unlink()
        log(f"+ {lay.rel(readme)} (project README)")

    title = title or project
    for f in (SITE / "hugo.yaml", readme):
        if f.exists() and "__PROJECT_TITLE__" in (text := f.read_text(encoding="utf-8")):
            f.write_text(text.replace("__PROJECT_TITLE__", title), encoding="utf-8")
            log(f'+ {lay.rel(f)}: title "{title}"')

    hugo_yaml = SITE / "hugo.yaml"
    text = hugo_yaml.read_text(encoding="utf-8")
    if "__REPO_URL__" in text:
        remote = git("remote", "get-url", "origin", check=False) if has_remote() else ""
        m = re.match(r"git@([^:]+):(.+)$", remote)
        url = f"https://{m.group(1)}/{m.group(2)}" if m else remote if remote.startswith("https://") else ""
        url = re.sub(r"\.git$", "", url)
        if url:
            hugo_yaml.write_text(text.replace("__REPO_URL__", url), encoding="utf-8")
            log(f"+ {site_rel}/hugo.yaml: repo {url}")
        else:
            log("NOTE: no 'origin' remote yet; re-run `hw init` after adding one (or edit site/hugo.yaml)")

    if board_is_open(project):
        log("NOTE: board is open in KiCad; skipped adding the drawing layers (re-run init after closing it)")
    else:
        setup_layout("--layers-only")
    log(f"Done ({'flat project' if lay.hw == lay.root else 'hardware in ' + lay.hw_rel + '/'}, "
        f"site in {site_rel}/). Next: hw kibot build host")


def has_remote():
    return "origin" in git("remote", check=False).split()


VENDORED = [
    # (url, destination relative to site/, label). KiCanvas has no versioned builds.
    ("https://kicanvas.org/kicanvas/kicanvas.js", "static/vendor/kicanvas/kicanvas.js", "KiCanvas"),
    ("https://cdn.jsdelivr.net/npm/online-3d-viewer@0.18.0/build/engine/o3dv.min.js",
     "static/vendor/o3dv/o3dv.min.js", "Online3DViewer 0.18.0"),
    ("https://cdn.jsdelivr.net/npm/mermaid@12.2.0/dist/mermaid.min.js", "assets/js/mermaid.min.js", "Mermaid 12.2.0"),
]


def vendor():
    """Refresh the vendored browser libraries (bump the pinned versions above to upgrade)."""
    from datetime import date
    for url, rel_dst, label in VENDORED:
        dst = SITE / rel_dst
        with urllib.request.urlopen(url) as r:
            data = r.read()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        source = dst.parent / ("SOURCE.txt" if "static/vendor" in rel_dst else dst.name.split(".")[0] + ".SOURCE.txt")
        source.write_text(f"{label} from {url} ({date.today()})\nsha256 {hashlib.sha256(data).hexdigest()}\n"
                          "Refresh with: hw vendor\n", encoding="utf-8")
        log(f"Updated {rel_dst} ({len(data)} bytes)")


def clean():
    lay = L()
    for p in (SITE / "public", SITE / "resources", lay.gen, lay.www, lay.build / "work", lay.build / "tags"):
        rmtree(p)
    for p in (SITE / "config_override.yaml", SITE / ".hugo_build.lock"):
        p.unlink(missing_ok=True)
    for d in STAGED_DIRS:
        rmtree(SITE / "static" / d)
    log("Cleaned (build/releases kept)")


# ── Template export ──────────────────────────────────────────────────────────
BLANK_PROJECT_PY = r'''
import sys, uuid, pcbnew
d, name = sys.argv[1], sys.argv[2]
mm = pcbnew.FromMM
board = pcbnew.NewBoard(f"{d}/{name}.kicad_pcb")      # also writes .kicad_pro
outline = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
outline.SetStart(pcbnew.VECTOR2I(mm(100), mm(70)))
outline.SetEnd(pcbnew.VECTOR2I(mm(150), mm(120)))
outline.SetLayer(pcbnew.Edge_Cuts)
outline.SetWidth(mm(0.05))
board.Add(outline)
# Four M3 mounting holes: realistic placeholder, and the drill table needs holes.
for i, (x, y) in enumerate([(104, 74), (146, 74), (104, 116), (146, 116)], 1):
    fp = pcbnew.FootprintLoad("/usr/share/kicad/footprints/MountingHole.pretty", "MountingHole_3.2mm_M3")
    fp.SetReference(f"H{i}")
    fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    fp.SetBoardOnly(True)            # no schematic symbol needed
    fp.SetExcludedFromBOM(True)
    fp.SetExcludedFromPosFiles(True)
    board.Add(fp)
pcbnew.SaveBoard(f"{d}/{name}.kicad_pcb", board)
open(f"{d}/{name}.kicad_sch", "w").write(
    '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n'
    f'\t(uuid "{uuid.uuid4()}")\n\t(paper "A4")\n\t(lib_symbols)\n'
    '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n')
'''


def make_deterministic(path):
    """Stable UUIDs and footprint order, so re-exports of an unchanged template are identical."""
    text = path.read_text(encoding="utf-8")
    fps = re.findall(r"\n\t\(footprint .*?\n\t\)", text, re.S)
    if fps:   # KiCad saves footprints in (random) UUID order: sort by reference
        start = text.index(fps[0])
        rest = text[start:]
        for fp in fps:
            rest = rest.replace(fp, "", 1)
        ref = lambda fp: re.search(r'\(property "Reference" "([^"]*)"', fp).group(1)  # noqa: E731
        text = text[:start] + "".join(sorted(fps, key=ref)) + rest
    counter = iter(range(10 ** 6))
    text = re.sub(r'\(uuid "[0-9a-f-]+"\)',
                  lambda m: '(uuid "%s")' % uuid.uuid5(uuid.NAMESPACE_URL,
                                                        "hw-pipeline/%s/%d" % (path.name, next(counter))),
                  text)
    path.write_text(text, encoding="utf-8")


def export_template(dest):
    """Write this tooling as a KiCad project template: a blank KiCad 10 project
    named `hwproject` (KiCad renames it on project creation) plus the KiBot
    config, drawing templates, site/ and CI source. No design files from this
    project are included. DEST is replaced only after a successful build, and
    DEST/.git is kept so the template can be its own repository."""
    final = Path(dest).expanduser().resolve()
    if final.exists() and any(final.iterdir()) and not (final / f"{TEMPLATE_BASE}.kicad_pro").exists() \
            and not (final / "meta" / "info.html").exists():
        raise HwError(f"{final} exists and isn't a previous template export; refusing to overwrite")
    project = project_name()
    # Stage under build/ (not the system temp dir, which Docker Desktop may not share).
    lay = L()
    if not (SITE / "template").is_dir():
        raise HwError(f"{SITE / 'template'} is missing (the template's own READMEs and pages); "
                      "copy it from the template repository")
    lay.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="template-staging.", dir=lay.build) as tmp:
        out = Path(tmp)
        (out / "meta").mkdir()
        (out / "site" / "content").mkdir(parents=True)
        (out / "site" / "static").mkdir()

        # Tooling, copied as-is
        for name in (".gitignore", ".gitattributes"):
            src = lay.hw / name if (lay.hw / name).exists() else lay.root / name
            if src.exists():
                shutil.copy2(src, out / name)
        (out / "hw").write_text(launcher_sh("site"), encoding="utf-8", newline="")   # template is flat
        os.chmod(out / "hw", 0o755)
        (out / "hw.cmd").write_text(launcher_cmd("site"), encoding="utf-8", newline="")
        shutil.copytree(lay.hw / "kibot_templates", out / "kibot_templates")
        shutil.copy2(lay.hw / f"{project}.kibot.yaml", out / f"{TEMPLATE_BASE}.kibot.yaml")
        for name in ("hw.py", "go.mod", "go.sum"):
            shutil.copy2(SITE / name, out / "site" / name)
        for name in ("assets", "ci", "layouts", "scripts", "template"):   # template: so any project can re-export
            shutil.copytree(SITE / name, out / "site" / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SITE / "static" / "vendor", out / "site" / "static" / "vendor")
        for name in ("board.md", "drawings.md", "bill-of-materials.md", "release.md"):   # project-neutral pages
            shutil.copy2(SITE / "content" / name, out / "site" / "content" / name)
        shutil.copytree(SITE / "template" / "design", out / "site" / "content" / "design")
        shutil.copytree(SITE / "content" / "software", out / "site" / "content" / "software")
        for name, target in (("TEMPLATE_README.md", out / "README.md"),
                             ("README.project.md", out / "site" / "README.project.md"),
                             ("template_info.html", out / "meta" / "info.html")):
            shutil.copy2(SITE / "template" / name, target)
        shutil.copy2(SITE / "template" / "_index.md", out / "site" / "content" / "_index.md")

        # hugo.yaml with placeholders
        y = (SITE / "hugo.yaml").read_text(encoding="utf-8")
        for pattern, repl in [(r"^title: .*", 'title: "__PROJECT_TITLE__"'),
                              (r"^(  projectName: ).*", r'\1"__PROJECT_TITLE__"'),
                              (r"^(  description: ).*", r'\1"KiCad hardware project"'),
                              (r"^(  repoURL: ).*", r'\1"__REPO_URL__"'),
                              (r"^(  license: ).*", r'\1""'),
                              (r"^(      url: )https://github.com/.*", r"\1__REPO_URL__")]:
            y = re.sub(pattern, repl, y, flags=re.M)
        (out / "site" / "hugo.yaml").write_text(y, encoding="utf-8")

        # Blank KiCad 10 project: placeholder 50 x 50 mm outline + 4 M3 holes
        (out / ".mkblank.py").write_text(BLANK_PROJECT_PY, encoding="utf-8")
        docker(KIBOT_IMAGE, ["python3", ".mkblank.py", "/w", TEMPLATE_BASE],
               mounts=[(out, "/w")], workdir="/w", drop="assert", label="blank project")
        (out / ".mkblank.py").unlink()
        (out / f"{TEMPLATE_BASE}.kicad_prl").unlink(missing_ok=True)
        pcb = out / f"{TEMPLATE_BASE}.kicad_pcb"
        if not pcb.exists() or not (out / f"{TEMPLATE_BASE}.kicad_pro").exists():
            raise HwError("blank project was not created")
        # B-size sheet (PAGE_INFO isn't usable from Python)
        pcb.write_text(pcb.read_text(encoding="utf-8").replace('(paper "A4")', '(paper "B")'), encoding="utf-8")
        make_deterministic(pcb)
        make_deterministic(out / f"{TEMPLATE_BASE}.kicad_sch")

        # Replace DEST contents (keeping .git)
        final.mkdir(parents=True, exist_ok=True)
        for p in final.iterdir():
            if p.name != ".git":
                rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(out, final, dirs_exist_ok=True)
    log(f"Template written to {final}")


# ── CLI ──────────────────────────────────────────────────────────────────────
COMMANDS = ["init", "kibot", "build", "serve", "host", "release", "pin-layout", "unpin-layout",
            "vendor", "export-template", "clean", "versions", "help"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hw", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    ap.add_argument("commands", nargs="*", metavar="command", help="see below")
    ap.add_argument("--tag", default=os.environ.get("TAG"), help="release tag (release)")
    ap.add_argument("--strict", dest="strict", action="store_true", default=None,
                    help="fail release on ERC/DRC errors (default)")
    ap.add_argument("--no-strict", dest="strict", action="store_false",
                    help="release even with ERC/DRC errors")
    ap.add_argument("--base-path", default=os.environ.get("BASE_PATH", ""),
                    help="URL prefix when hosted below a domain root, e.g. /<repo> on github.io")
    ap.add_argument("--host", default=os.environ.get("HOST_ADDR", "127.0.0.1"), help="address for host/serve")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="port for host")
    ap.add_argument("--dest", default=os.environ.get("DEST"), help="export-template destination")
    ap.add_argument("--title", default=os.environ.get("TITLE"), help="site title (init)")
    ap.add_argument("--flat", action="store_true",
                    help="init: keep a new project flat instead of moving it into hardware/ (monorepo)")
    ap.add_argument("--www", default=None, help="site folder (versions; default build/www)")
    a = ap.parse_args(argv)
    if not a.commands or "help" in a.commands:
        ap.print_help()
        return 0
    unknown = [c for c in a.commands if c not in COMMANDS]
    if unknown:
        ap.error(f"unknown command(s): {', '.join(unknown)}")
    strict = a.strict if a.strict is not None else os.environ.get("STRICT", "1") != "0"
    try:
        for c in a.commands:
            if c == "init":
                init(a.title, a.flat)
            elif c == "kibot":
                kibot("WORKTREE", L().gen)
            elif c == "build":
                build(a.base_path)
            elif c == "serve":
                serve(a.base_path, a.host)
            elif c == "host":
                host(a.host, a.port)
            elif c == "release":
                release(a.tag, strict, a.base_path)
            elif c == "pin-layout":
                setup_layout("--pin")
            elif c == "unpin-layout":
                setup_layout("--unpin")
            elif c == "vendor":
                vendor()
            elif c == "export-template":
                export_template(a.dest or L().build / "template" / "hw-pipeline")
            elif c == "clean":
                clean()
            elif c == "versions":
                versions(a.www)
    except HwError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        if e.filename in ("docker", "git"):
            print(f"ERROR: `{e.filename}` not found; install it and make sure it's on PATH", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
