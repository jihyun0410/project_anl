"""Indented call tree (├─ └─) as HTML rows; branch conditions in red, every row clickable via data-id."""
import re
from html import escape


def build(funcs, fid, cap=300):
    """Unfold the call graph from fid into a tree; cycles are cut, at most cap nodes. -> (root, truncated)"""
    left = [cap]

    def rec(fid, conds, path):
        n = {"f": funcs[fid], "conds": [_cut(c, 50) for c in conds], "loop": fid in path, "kids": []}
        for cid, cc in [] if n["loop"] else funcs[fid].children:
            left[0] -= 1
            if left[0] < 0:
                break
            n["kids"].append(rec(cid, cc, path | {fid}))
        return n

    return rec(fid, [], frozenset()), left[0] < 0


def _cut(s, k=60):
    return s if len(s) <= k else s[:k - 1] + "…"


def render(root):
    rows = []

    def rec(n, pre, tail):
        f = n["f"]
        # branch conditions in red (ct-c); HTTP hops ("GET http://..", upper-case verb) stay plain
        conds = " › ".join(escape(c) if re.match(r"[A-Z]+ ", c) else f'<span class="ct-c">{escape(c)}</span>'
                           for c in n["conds"])
        tip = escape(f.name + f.params + "\n" + f"{f.file}:{f.line}")  # full path lives in the tooltip
        rows.append(f'<div class="ct-r" data-id="{escape(f.id)}" title="{tip}"><span class="ct-p">{pre}{tail}</span>'
                    + (conds + " " if conds else "") + f'<b>{escape(_cut(f.name))}</b>{escape(_cut(f.params))} '
                    f'<span class="ct-s">{escape(f.file.rsplit("/", 1)[-1])}:{f.line}</span>'
                    + (" ↻" if n["loop"] else "") + "</div>")
        sub = pre + ("" if not tail else "   " if tail == "└─ " else "│  ")
        for i, k in enumerate(n["kids"]):
            rec(k, sub, "└─ " if i == len(n["kids"]) - 1 else "├─ ")

    rec(root, "", "")
    return "".join(rows)


if __name__ == "__main__":
    from types import SimpleNamespace as F
    fs = {k: F(id=k, name=f"Svc.{k}", params="(String id)", file="a/Svc.java", line=1, children=[]) for k in "rabcdeg"}
    fs["r"].children = [("a", ["if (x)"]), ("b", []), ("c", ["GET http://eqps:8080/api/eqp/{}"])]
    fs["a"].children = [("d", []), ("e", []), ("r", ["else (if y)"])]  # r: back to the root -> cycle
    fs["b"].children = [("g", [])]
    root, cut = build(fs, "r")
    html = render(root)
    rows = html.split("</div>")[:-1]
    assert [re.search(r'data-id="(\w)"', r)[1] for r in rows] == list("raderbgc")
    assert [re.search(r'"ct-p">([^<]*)<', r)[1] for r in rows] == \
        ["", "├─ ", "│  ├─ ", "│  ├─ ", "│  └─ ", "├─ ", "│  └─ ", "└─ "]
    assert not cut and rows[4].endswith(" ↻") and build(fs, "r", cap=3)[1]
    assert '<span class="ct-c">if (x)</span> <b>Svc.a</b>' in html and "</span>GET http://eqps:8080/api/eqp/{} <b>" in html
    print("ok")
