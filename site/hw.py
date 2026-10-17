#!/usr/bin/env python3
"""hw: build, preview and release this KiCad project (Linux, macOS, Windows).

    ./hw <command> [<command> ...] [options]        (Linux/macOS)
    hw <command> [<command> ...] [options]          (Windows: hw.cmd)
    python3 site/hw.py <command> ...                (anywhere)

Commands run in the order given, e.g. `hw kibot build host`.

  init             one-time setup after creating a project from the template
  kibot            run KiBot on the working tree (incl. uncommitted changes) -> generated/
  build            build the site for the working tree -> build/www/latest
  serve            live-reload site at http://localhost:1313/
  host             serve every built version at http://127.0.0.1:8000/
  release          --tag vX.Y.Z: strict ERC/DRC, packages in build/releases/, versioned site
  pin-layout       copy the drawing layout into the board to adjust by hand (KiCad closed)
  unpin-layout     remove pinned drawing placeholders; back to automatic layout
  vendor-kicanvas  refresh the vendored KiCanvas viewer from kicanvas.org
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
REPO = SITE.parent
SCRIPTS = SITE / "scripts"
BUILD = REPO / "build"
GEN = REPO / "generated"
WWW = BUILD / "www"
RELEASES = BUILD / "releases"
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


def git(*args, cwd=REPO, check=True):
    return run(["git", *args], cwd=cwd, check=check, capture=True)


def has_commits(repo):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=repo,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def project_name(root=REPO):
    if os.environ.get("PROJECT"):
        return os.environ["PROJECT"]
    pros = sorted(Path(root).glob("*.kicad_pro"))
    if len(pros) != 1:
        raise HwError(f"expected exactly one .kicad_pro in {root} (found {len(pros)}); set PROJECT=<name>")
    return pros[0].stem


def board_is_open(project):
    return (REPO / f"~{project}.kicad_pcb.lck").exists()


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
    """Copy tracked + untracked (non-ignored) files of the working tree to dst."""
    files = git("ls-files", "-z", "-co", "--exclude-standard").split("\0")
    for f in filter(None, files):
        src = REPO / f
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
    project = project_name()
    work = BUILD / "work" / ref.replace("/", "_")
    cache = BUILD / "cache_3d"
    rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    if ref == "WORKTREE" and not has_commits(REPO):
        # Brand-new repo without commits: snapshot the working tree into a scratch repo.
        work.mkdir()
        copy_worktree(work)
        git("init", "-q", cwd=work)
        git("add", "-A", cwd=work)
        git("-c", "user.name=kibot", "-c", "user.email=kibot@localhost", "commit", "-q", "-m", "snapshot",
            cwd=work)
    elif ref == "WORKTREE":
        # Local clone: has every tag (diffs, git describe) and is cheap.
        git("clone", "-q", "--local", "--no-checkout", str(REPO), str(work))
        git("checkout", "-q", "--detach", git("rev-parse", "HEAD"), cwd=work)
        copy_worktree(work)
    else:
        git("clone", "-q", "--local", "--no-checkout", str(REPO), str(work))
        git("checkout", "-q", "--detach", ref, cwd=work)
        cfg = f"{project}.kibot.yaml"
        if not (work / cfg).exists():   # tags made before the pipeline existed
            log(f"NOTE: {ref} has no {cfg}; using the working tree's copy")
            shutil.copy2(REPO / cfg, work / cfg)
    if not (work / "kibot_templates").exists():
        shutil.copytree(REPO / "kibot_templates", work / "kibot_templates")

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
           mounts=[(work, "/w"), (SCRIPTS, "/scripts", True)], workdir="/w", drop="assert",
           label="drawing layout")

    base = f"-c {project}.kibot.yaml -e {project}.kicad_sch -b {project}.kicad_pcb -d /out {' '.join(defs)}"
    script = (f"set -e; git config --global --add safe.directory '*'; kibot {base}; "
              # Diffs are best-effort (e.g. no earlier tag to compare against).
              f"for t in diff_sch diff_pcb; do kibot {base} -s run_erc,run_drc $t "
              f"|| echo \"WARNING: $t failed (no previous tag?)\"; done")
    docker(KIBOT_IMAGE, ["bash", "-c", script],
           mounts=[(work, "/w"), (out, "/out"), (cache, "/cache_3d")], workdir="/w",
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
        assembly = [{"name": "pcb", "file": glb.name}]

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
    (SITE / "config_override.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {SITE / 'config_override.yaml'}")


def hugo(shell_cmd, ports=()):
    WWW.mkdir(parents=True, exist_ok=True)
    docker(HUGO_IMAGE, ["sh", "-c", f"hugo mod get && {shell_cmd}"],
           mounts=[(SITE, "/src"), (BUILD / "hugo_cache", "/cache"), (WWW, "/www")],
           workdir="/src", env={"HUGO_CACHEDIR": "/cache"}, ports=ports, label="Hugo")


def versions(www=WWW):
    """versions.json + redirecting index.html at the hosting root (gh-pages layout)."""
    www = Path(www)

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
    prepare(GEN, REPO, "latest", f"{base_path}/latest/")
    hugo("hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/latest")
    versions()


def serve(base_path, host_addr):
    prepare(GEN, REPO, "latest", f"{base_path}/latest/")
    hugo("hugo server -D --bind 0.0.0.0 --baseURL http://localhost:1313/ "
         "--config hugo.yaml,config_override.yaml", ports=[f"{host_addr}:1313:1313"])


def host(host_addr, port):
    WWW.mkdir(parents=True, exist_ok=True)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(WWW))
    try:
        httpd = http.server.ThreadingHTTPServer((host_addr, port), handler)
    except OSError as e:
        raise HwError(f"can't serve on {host_addr}:{port} ({e.strerror}); is another `hw host` "
                      f"already running? Stop it, or use --port <n>")
    log(f"Serving {WWW} at http://{host_addr}:{port}/  (Ctrl-C to stop)")
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
    gen = BUILD / "tags" / tag / "generated"
    kibot(tag, gen, strict=strict)
    package(gen, project_name(), tag, RELEASES / tag)
    prepare(gen, BUILD / "work" / tag, tag, f"{base_path}/{tag}/")
    hugo(f"hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/{tag}")
    versions()


# ── Board layout (real board: init / pin / unpin) ───────────────────────────
def setup_layout(mode):
    project = project_name()
    if board_is_open(project):
        raise HwError("close the board in KiCad first")
    docker(KIBOT_IMAGE, ["python3", "site/scripts/setup_drawing_layers.py",
                         f"{project}.kicad_pcb", f"{project}.kibot.yaml", mode],
           mounts=[(REPO, "/w")], workdir="/w", drop="assert", label="board layout")


def init(title):
    """One-time setup for a project created from the template. Safe to re-run."""
    project = project_name()
    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        git("init", "-q")
        log("+ git init")

    ci_src, ci_dst = SITE / "ci" / "ci.yml", REPO / ".github" / "workflows" / "ci.yml"
    if not ci_dst.exists() or ci_dst.read_bytes() != ci_src.read_bytes():
        ci_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ci_src, ci_dst)
        log("+ .github/workflows/ci.yml")

    readme, project_readme = REPO / "README.md", SITE / "README.project.md"
    if readme.exists() and "hw-pipeline template README" in readme.read_text(encoding="utf-8") \
            and project_readme.exists():
        shutil.move(str(project_readme), str(readme))
        log("+ README.md (project README)")

    title = title or project
    for f in (SITE / "hugo.yaml", readme):
        if f.exists() and "__PROJECT_TITLE__" in (text := f.read_text(encoding="utf-8")):
            f.write_text(text.replace("__PROJECT_TITLE__", title), encoding="utf-8")
            log(f'+ {f.relative_to(REPO).as_posix()}: title "{title}"')

    hugo_yaml = SITE / "hugo.yaml"
    text = hugo_yaml.read_text(encoding="utf-8")
    if "__REPO_URL__" in text:
        remote = git("remote", "get-url", "origin", check=False) if has_remote() else ""
        m = re.match(r"git@([^:]+):(.+)$", remote)
        url = f"https://{m.group(1)}/{m.group(2)}" if m else remote if remote.startswith("https://") else ""
        url = re.sub(r"\.git$", "", url)
        if url:
            hugo_yaml.write_text(text.replace("__REPO_URL__", url), encoding="utf-8")
            log(f"+ site/hugo.yaml: repo {url}")
        else:
            log("NOTE: no 'origin' remote yet; re-run `hw init` after adding one (or edit site/hugo.yaml)")

    if board_is_open(project):
        log("NOTE: board is open in KiCad; skipped adding the drawing layers (re-run init after closing it)")
    else:
        setup_layout("--layers-only")
    log("Done. Next: hw kibot build host")


def has_remote():
    return "origin" in git("remote", check=False).split()


def vendor_kicanvas():
    dst = SITE / "static" / "vendor" / "kicanvas" / "kicanvas.js"
    with urllib.request.urlopen("https://kicanvas.org/kicanvas/kicanvas.js") as r:
        data = r.read()
    dst.write_bytes(data)
    from datetime import date
    (dst.parent / "SOURCE.txt").write_text(
        f"Vendored from https://kicanvas.org/kicanvas/kicanvas.js on {date.today()}\n"
        f"sha256 {hashlib.sha256(data).hexdigest()}\nRefresh with: hw vendor-kicanvas\n", encoding="utf-8")
    log(f"Updated {dst} ({len(data)} bytes)")


def clean():
    for p in (SITE / "public", SITE / "resources", GEN, WWW, BUILD / "work", BUILD / "tags"):
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
    BUILD.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="template-staging.", dir=BUILD) as tmp:
        out = Path(tmp)
        (out / "meta").mkdir()
        (out / "site" / "content").mkdir(parents=True)
        (out / "site" / "static").mkdir()

        # Tooling, copied as-is
        for name in (".gitignore", ".gitattributes", "hw", "hw.cmd"):
            if (REPO / name).exists():
                shutil.copy2(REPO / name, out / name)
        shutil.copytree(REPO / "kibot_templates", out / "kibot_templates")
        shutil.copy2(REPO / f"{project}.kibot.yaml", out / f"{TEMPLATE_BASE}.kibot.yaml")
        for name in ("hw.py", "go.mod", "go.sum"):
            shutil.copy2(SITE / name, out / "site" / name)
        for name in ("ci", "layouts", "scripts"):
            shutil.copytree(SITE / name, out / "site" / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SITE / "static" / "vendor", out / "site" / "static" / "vendor")
        for name in ("board.md", "release.md"):
            shutil.copy2(SITE / "content" / name, out / "site" / "content" / name)
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
            "vendor-kicanvas", "export-template", "clean", "versions", "help"]


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
    ap.add_argument("--www", default=str(WWW), help="site folder (versions)")
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
                init(a.title)
            elif c == "kibot":
                kibot("WORKTREE", GEN)
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
            elif c == "vendor-kicanvas":
                vendor_kicanvas()
            elif c == "export-template":
                export_template(a.dest or BUILD / "template" / "hw-pipeline")
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
