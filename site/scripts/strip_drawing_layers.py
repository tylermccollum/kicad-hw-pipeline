"""Copy a .kicad_pcb without the fab/assembly drawing annotations.

    python3 strip_drawing_layers.py <in.kicad_pcb> <out.kicad_pcb>

Used for the KiCanvas viewer copy only: drops every top-level item on the
drawing layers created by setup_drawing_layers.py (matched by layer name, so
any User.N slot works) plus the kibot_table_*/kibot_image_* groups.
Footprints are kept even if they have graphics on those layers.
"""
import re
import sys

DRAWING_LAYERS = {"DrillMap", "FabNotes", "Dimensions", "AsmNotes", "AsmCover"}


def top_level_items(text):
    """Yield (start, end) spans of the board's direct children `(...)`."""
    depth, start, i, n = 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':                      # skip strings, honouring \" escapes
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2:
                start = i
        elif c == ")":
            if depth == 2:
                yield start, i + 1
            depth -= 1
        i += 1


src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()

# Canonical layer ids (e.g. "User.2") whose user name is a drawing layer.
canon = {m.group(1) for m in re.finditer(r'\(\d+ "([^"]+)" user "([^"]+)"\)', text)
         if m.group(2) in DRAWING_LAYERS}
layer_re = re.compile(r'\(layer "(%s)"' % "|".join(map(re.escape, canon))) if canon else None

out, last, dropped = [], 0, 0
for s, e in top_level_items(text):
    item = text[s:e]
    head = item[1:item.find(" ", 1) if " " in item[:40] else 40].strip()
    drop = (head == "group" and re.match(r'\(group "kibot_(table|image)_', item)) or \
           (layer_re and head != "footprint" and layer_re.search(item))
    if drop:
        # also eat the indentation/newline that preceded the item
        ws = s
        while ws > last and text[ws - 1] in " \t":
            ws -= 1
        if ws > last and text[ws - 1] == "\n":
            ws -= 1
        out.append(text[last:ws])
        last = e
        dropped += 1
out.append(text[last:])
open(dst, "w", encoding="utf-8").write("".join(out))
print(f"{dst}: dropped {dropped} drawing items")
