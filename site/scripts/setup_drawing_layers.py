"""Lay out a board for the KiBot fab/assembly drawings (pcb_print).

    python3 setup_drawing_layers.py <board.kicad_pcb> <project.kibot.yaml> [MODE]

MODE
  (none)         Build mode. kibot.sh runs this on its throwaway copy of the
                 project before every KiBot run; your real files are never
                 touched. Adds the drawing layers, picks the scale, writes the
                 scale values into the (throwaway) KiBot config, and adds any
                 missing placeholders and outline dimensions.
  --layers-only  Just add/name the drawing layers (DrillMap, FabNotes,
                 Dimensions, AsmNotes, AsmCover). `make -C site init`.
  --pin          Write the current automatic layout into the real board so it
                 can be adjusted in the PCB editor, and fix the scale
                 (DRAWING_SCALE_MODE: fixed). `make -C site pin-layout`.
  --unpin        Remove the placeholders, cover caption and generated outline
                 dimensions from the real board and return to automatic
                 scaling. `make -C site unpin-layout`.

Placeholders are board groups named kibot_table_<output> / kibot_image_<output>;
KiBot draws each table or image at its group. Any group already on the board
(i.e. pinned) is used as placed; only missing ones are generated.

Scale: with DRAWING_SCALE_MODE: auto the largest standard scale is chosen at
which the board, its dimensions, the notes block, the data tables and the
title block fit the sheet; TABLE_FONT is the table text size in board mm so it
always prints ~2 mm wide. With `fixed`, DRAWING_SCALE is used as set.
"""
import os
import re
import sys
import pcbnew

args = [a for a in sys.argv[1:] if not a.startswith("--")]
board_file, kibot_file = args[0], args[1]
flags = [a for a in sys.argv[1:] if a.startswith("--")]
mode = flags[0][2:] if flags else "build"
assert mode in ("build", "layers-only", "pin", "unpin"), f"unknown mode {mode}"
CAPTION = "ISOMETRIC VIEW - NOT TO SCALE"
board = pcbnew.LoadBoard(board_file)
mm, to_mm = pcbnew.FromMM, pcbnew.ToMM
cfg = open(kibot_file).read()

# ── Layers ───────────────────────────────────────────────────────────────────
LAYERS = {"DrillMap": pcbnew.User_1, "FabNotes": pcbnew.User_2,
          "Dimensions": pcbnew.User_3, "AsmNotes": pcbnew.User_4, "AsmCover": pcbnew.User_5}
enabled = board.GetEnabledLayers()
for name, lid in LAYERS.items():
    if not enabled.Contains(lid):
        enabled.AddLayer(lid)
        print(f"+ layer {board.GetLayerName(lid)} -> {name}")
board.SetEnabledLayers(enabled)
for name, lid in LAYERS.items():
    board.SetLayerName(lid, name)


def get_def(name):
    m = re.search(rf"^  {name}: *(.+?) *$", cfg, re.M)
    return m.group(1) if m else None


def set_def(name, value):
    global cfg
    if get_def(name) is None:
        cfg = cfg.rstrip("\n") + f"\n  {name}: {value}\n"
    else:
        cfg = re.sub(rf"^(  {name}:).*$", rf"\g<1> {value}", cfg, flags=re.M)


def save(write_cfg=False):
    if write_cfg:
        open(kibot_file, "w").write(cfg)
    board.Save(board_file)


if mode == "layers-only":
    save()
    sys.exit(0)

# ── Board and sheet geometry ─────────────────────────────────────────────────
bb = board.GetBoardEdgesBoundingBox()
bx0, by0, bx1, by1 = (to_mm(v) for v in (bb.GetX(), bb.GetY(), bb.GetRight(), bb.GetBottom()))
cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
bw_mm, bh_mm = bx1 - bx0, by1 - by0
# PAGE_INFO isn't usable from the Python bindings: read `(paper ...)` instead.
PAPER_MM = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420),
            "A1": (841, 594), "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4),
            "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
            "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4)}
paper = re.search(r'\(paper "([^"]+)"([^)]*)\)', open(board_file).read())
if paper.group(1) == "User":
    pw, ph = (float(v) for v in paper.group(2).split()[:2])
else:
    pw, ph = PAPER_MM[paper.group(1)]
    if "portrait" in paper.group(2):
        pw, ph = ph, pw


def is_outline_dim(d):
    """A generated outline dimension: along the board's top or left edge."""
    if not isinstance(d, pcbnew.PCB_DIM_ALIGNED) or d.GetLayer() != LAYERS["Dimensions"]:
        return False
    ends = tuple((round(to_mm(p.x), 3), round(to_mm(p.y), 3)) for p in (d.GetStart(), d.GetEnd()))
    edges = [((bx0, by0), (bx1, by0)), ((bx0, by1), (bx0, by0))]
    return ends in [tuple((round(x, 3), round(y, 3)) for x, y in e) for e in edges]


def placeholder_groups():
    return [g for g in board.Groups() if g.GetName().startswith(("kibot_table_", "kibot_image_"))]


if mode == "unpin":
    # Collect everything before removing: the bindings invalidate group
    # references once the board changes.
    doomed = [(g, list(g.GetItems())) for g in placeholder_groups()]
    loose = [d for d in board.GetDrawings()
             if (isinstance(d, pcbnew.PCB_TEXT) and d.GetText() == CAPTION) or is_outline_dim(d)]
    for g, items in doomed:
        print(f"- removed {g.GetName()}")
        for item in items:
            g.RemoveItem(item)
            board.Remove(item)
        board.Remove(g)
    for d in loose:
        board.Remove(d)
    if loose:
        print("- removed cover caption / outline dimensions")
    set_def("DRAWING_SCALE_MODE", "auto")
    print("= DRAWING_SCALE_MODE: auto")
    save(write_cfg=True)
    sys.exit(0)

# ── Scale ────────────────────────────────────────────────────────────────────
PAGE_FONT = 2.0                 # table/notes character width on paper (mm)
ROW_H = 1.9 * PAGE_FONT         # table row pitch on paper, measured from KiBot output
MARGIN = 12                     # sheet frame
GAP = 15                        # board edge (incl. dimensions) to tables
RIGHT_TABLE_W = 80              # fab data / component count tables
TITLE_W, TITLE_H = 140, 45      # KiCad title block, bottom right
SCALES = [5, 4, 3, 2, 1.5, 1, 0.5, 0.25, 0.2, 0.1]

templates = os.path.join(os.path.dirname(os.path.abspath(kibot_file)), "kibot_templates")
try:
    notes_rows = sum(1 for line in open(os.path.join(templates, "fab_instructions.csv")) if line.strip())
except OSError:
    notes_rows = 13
NOTES_BOTTOM = 25 + notes_rows * ROW_H + 5      # top-left notes block on the fab top view


def fits(scale):
    w, h = scale * bw_mm, scale * bh_mm
    left, right, top, bottom = pw / 2 - w / 2, pw / 2 + w / 2, ph / 2 - h / 2, ph / 2 + h / 2
    if top - GAP < NOTES_BOTTOM:                               # width dimension + notes
        return False
    if left - GAP < MARGIN or bottom > ph - MARGIN:            # height dimension, frame
        return False
    if right + GAP + RIGHT_TABLE_W > pw - MARGIN:              # data table right of board
        return False
    if bottom > ph - MARGIN - TITLE_H and right > pw - MARGIN - TITLE_W:
        return False                                           # title block
    return True


pinned = [g.GetName() for g in placeholder_groups()]
if (get_def("DRAWING_SCALE_MODE") or "auto") == "auto":
    scale = next((sc for sc in SCALES if fits(sc)), SCALES[-1])
    if not fits(scale):
        print(f"WARNING: a {bw_mm:.0f} x {bh_mm:.0f} mm board doesn't fit this sheet even at 1:10; "
              "use a larger sheet (Page Settings)")
    if pinned and mode == "build":
        print("NOTE: the board has pinned placeholders but DRAWING_SCALE_MODE is auto; "
              "they were placed for a fixed scale (make -C site unpin-layout to go fully automatic)")
else:
    scale = float(get_def("DRAWING_SCALE") or 2)
label = f"{scale:g}:1" if scale >= 1 else f"1:{1 / scale:g}"
if mode == "pin":
    set_def("DRAWING_SCALE_MODE", "fixed")
    set_def("DRAWING_SCALE", f"{scale:g}")
else:
    set_def("DRAWING_SCALE", f"{scale:g}")
    set_def("DRAWING_SCALE_LABEL", f"'{label}'")
    set_def("TABLE_FONT", f"{PAGE_FONT / scale:.4g}")
print(f"= drawing scale {label} for a {bw_mm:.1f} x {bh_mm:.1f} mm board on a {pw:g} x {ph:g} mm sheet"
      + (f" ({len(pinned)} pinned placeholders)" if pinned and mode == "build" else ""))


# ── Placeholders ─────────────────────────────────────────────────────────────
def board_xy(px, py):
    """Page position (mm, board centered on the sheet) -> board coordinates."""
    return cx + (px - pw / 2) / scale, cy + (py - ph / 2) / scale


def add_group(name, layer, x0, y0, x1, y1):
    r = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
    r.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0)))
    r.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    r.SetLayer(LAYERS[layer])
    r.SetWidth(mm(0.1))
    board.Add(r)
    g = pcbnew.PCB_GROUP(board)
    g.SetName(name)
    board.Add(g)
    g.AddItem(r)
    if mode == "pin":
        print(f"+ pinned {name} on {layer}")


right_of_board = pw / 2 + scale * bw_mm / 2 + GAP   # page x for data tables
top_of_board = ph / 2 - scale * bh_mm / 2           # page y of the board's top edge
TABLES = {
    # group suffix (= KiBot output name): (layer, page x, page y), board-centered pages
    "fab_notes": ("FabNotes", right_of_board, top_of_board),
    "fab_instructions": ("FabNotes", 25, 25),
    "drill_table": ("DrillMap", 25, 25),            # no notes on the drill page
    "asm_count": ("AsmNotes", right_of_board, top_of_board),
}
for suffix, (layer, px, py) in TABLES.items():
    name = f"kibot_table_{suffix}"
    if name not in pinned:
        x, y = board_xy(px, py)
        add_group(name, layer, x, y, x + 40, y + 1)

# Assembly cover page: notes block (left) + iso render (right). KiBot pastes
# images at raw sheet coordinates (no page scale/centering), so the cover page
# prints 1:1 uncentered and these items sit at absolute sheet coordinates.
if "kibot_table_asm_instructions" not in pinned:
    add_group("kibot_table_asm_instructions", "AsmCover", 25, 25, 65, 26)
if "kibot_image_asm_iso" not in pinned:
    img_w = pw * 0.45                      # 4:3 like the asm_iso render (it gets stretched)
    img_h = img_w * 3 / 4
    x1 = pw - 25
    add_group("kibot_image_asm_iso", "AsmCover", x1 - img_w, 25, x1, 25 + img_h)
if not any(isinstance(d, pcbnew.PCB_TEXT) and d.GetText() == CAPTION for d in board.GetDrawings()):
    img_w = pw * 0.45
    img_h = img_w * 3 / 4
    x1 = pw - 25
    t = pcbnew.PCB_TEXT(board)
    t.SetText(CAPTION)
    t.SetLayer(LAYERS["AsmCover"])
    t.SetPosition(pcbnew.VECTOR2I(mm(x1 - img_w / 2), mm(25 + img_h + 8)))
    t.SetTextSize(pcbnew.VECTOR2I(mm(3), mm(3)))
    t.SetTextThickness(mm(0.45))
    board.Add(t)

if not any(is_outline_dim(d) for d in board.GetDrawings()):
    for (xa, ya, xb, yb) in ((bx0, by0, bx1, by0), (bx0, by1, bx0, by0)):  # width above, height left
        d = pcbnew.PCB_DIM_ALIGNED(board, pcbnew.PCB_DIM_ALIGNED_T)
        d.SetLayer(LAYERS["Dimensions"])
        d.SetStart(pcbnew.VECTOR2I(mm(xa), mm(ya)))
        d.SetEnd(pcbnew.VECTOR2I(mm(xb), mm(yb)))
        d.SetHeight(mm(-8 / scale))                 # ~8 mm from the board on paper
        d.SetUnitsMode(pcbnew.DIM_UNITS_MODE_MM)
        d.SetPrecision(pcbnew.DIM_PRECISION_X_X)
        d.SetTextSize(pcbnew.VECTOR2I(mm(3 / scale), mm(3 / scale)))   # ~3 mm on paper
        d.SetTextThickness(mm(0.4 / scale))
        d.SetLineThickness(mm(0.25 / scale))
        board.Add(d)
    if mode == "pin":
        print("+ pinned outline dimensions")

save(write_cfg=True)
if mode == "pin":
    print(f"Pinned layout at {label} (DRAWING_SCALE_MODE: fixed). Adjust in the PCB editor; "
          "delete a group to make it automatic again.")
