"""Top-down call tree as SVG: boxes + curved arrows, branch conditions as edge labels."""
from html import escape

BOX_H, GAP_X, GAP_Y, PAD = 56, 28, 72, 20
CH = 7.2  # px per char, 12px monospace; CJK counts double. ponytail: estimated, measure in JS if boxes clip


def build(funcs, fid, cap=300):
    """Unfold the call graph from fid into a tree; cycles are cut, at most cap nodes. -> (root, truncated)"""
    left = [cap]

    def rec(fid, label, path):
        n = {"f": funcs[fid], "label": label, "loop": fid in path, "kids": []}
        for cid, conds in [] if n["loop"] else funcs[fid].children:
            left[0] -= 1
            if left[0] < 0:
                break
            n["kids"].append(rec(cid, " › ".join(conds), path | {fid}))
        return n

    return rec(fid, "", frozenset()), left[0] < 0


def _walk(root):
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n["kids"])


def _w(s):
    return len(s) + sum(ord(c) > 0x2E7F for c in s)


def _cut(s, k=60):
    return s if len(s) <= k else s[:k - 1] + "…"


def _lines(n):
    f = n["f"]
    return [_cut(f.name) + (" ↻" if n["loop"] else ""), _cut(f.params), f"{f.file.rsplit('/', 1)[-1]}:{f.line}"]


def _measure(n):
    n["box"] = max(map(_w, _lines(n))) * CH + PAD
    n["kw"] = sum(_measure(k) for k in n["kids"]) + GAP_X * max(len(n["kids"]) - 1, 0)
    n["span"] = max(n["box"], _w(_cut(n["label"], 70)) * CH, n["kw"])
    return n["span"]


def _place(n, x, y):
    n["cx"], n["y"] = x + n["span"] / 2, y
    x += (n["span"] - n["kw"]) / 2
    for k in n["kids"]:
        _place(k, x, y + BOX_H + GAP_Y)
        x += k["span"] + GAP_X


def render(root, uid):
    """SVG markup; every box carries data-id=<Func.id> for click handling. uid must be unique per page."""
    _measure(root)
    _place(root, 0, 4)
    nodes = list(_walk(root))
    edges, boxes = [], []
    for n in nodes:
        for k in n["kids"]:
            x1, y1, x2, y2 = round(n["cx"]), n["y"] + BOX_H, round(k["cx"]), k["y"]
            m = (y1 + y2) // 2
            edges.append(f'<path class="ct-e" d="M{x1},{y1} C{x1},{m} {x2},{m} {x2},{y2 - 2}" '
                         f'marker-end="url(#{uid})"/>')
            if k["label"]:
                edges.append(f'<text class="ct-l" x="{x2}" y="{y2 - 12}"><title>{escape(k["label"])}</title>'
                             f'{escape(_cut(k["label"], 70))}</text>')
        f, cx, y = n["f"], round(n["cx"]), n["y"]
        t1, t2, t3 = map(escape, _lines(n))
        tip = escape(f.name + f.params + "\n" + f"{f.file}:{f.line}")  # full path lives in the tooltip
        boxes.append(f'<g class="ct-n" data-id="{escape(f.id)}"><title>{tip}</title>'
                     f'<rect x="{round(n["cx"] - n["box"] / 2)}" y="{y}" width="{round(n["box"])}" '
                     f'height="{BOX_H}" rx="3"/><text class="ct-t" x="{cx}" y="{y + 18}">{t1}</text>'
                     f'<text x="{cx}" y="{y + 34}">{t2}</text><text class="ct-s" x="{cx}" y="{y + 49}">{t3}</text></g>')
    h = max(n["y"] for n in nodes) + BOX_H + 6
    return (f'<svg width="{round(root["span"])}" height="{h}" xmlns="http://www.w3.org/2000/svg"><defs>'
            f'<marker id="{uid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto"><path class="ct-a" d="M0,0L10,5L0,10z"/></marker></defs>'
            + "".join(edges) + "".join(boxes) + "</svg>")


if __name__ == "__main__":
    from types import SimpleNamespace as F
    fs = {k: F(id=k, name=f"Svc.{k}", params="(String id)", file="Svc.java", line=1, children=[]) for k in "rabcdeg"}
    fs["r"].children = [("a", ["if (x)"]), ("b", []), ("c", ["GET http://eqps:8080/api/eqp/{}"])]
    fs["a"].children = [("d", []), ("e", []), ("r", ["else (if y)"])]  # r: back to the root -> cycle
    fs["b"].children = [("g", [])]
    root, cut = build(fs, "r")
    svg = render(root, "t0")
    rows = {}
    for n in _walk(root):
        rows.setdefault(n["y"], []).append((n["cx"] - n["box"] / 2, n["cx"] + n["box"] / 2))
    for row in rows.values():  # boxes on one level never overlap
        row.sort()
        assert all(a[1] <= b[0] for a, b in zip(row, row[1:])), row
    a = root["kids"][0]
    assert not cut and a["kids"][2]["loop"] and not a["kids"][2]["kids"]
    assert a["kids"][0]["cx"] < a["cx"] < a["kids"][2]["cx"]  # parent sits above its children
    assert build(fs, "r", cap=3)[1]
    assert 'data-id="a"' in svg and "if (x)" in svg and "{}" in svg and len(rows) == 3
    print("ok")
