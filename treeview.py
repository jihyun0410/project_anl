"""Indented call tree (├─ └─) as HTML rows; branch conditions in red, every row clickable via data-id."""
import re
from html import escape


def build(funcs, fid, cap=300, once=False):
    """Unfold the call graph from fid into a tree; cycles are cut, at most cap nodes. -> (root, truncated)
    once: a func with calls unfolds only at its first row, later rows point up to it (size ~ call edges, no blowup)"""
    left, seen = [cap], set()

    def rec(fid, conds, site, path):
        loop = fid in path
        dup = once and not loop and fid in seen and bool(funcs[fid].children)
        seen.add(fid)
        n = {"f": funcs[fid], "conds": [_cut(c, 50) for c in conds], "site": site, "loop": loop, "dup": dup,
             "kids": []}
        for cid, cc, cs in [] if loop or dup else funcs[fid].children:
            left[0] -= 1
            if left[0] < 0:
                break
            n["kids"].append(rec(cid, cc, cs, path | {fid}))
        return n

    return rec(fid, [], None, frozenset()), left[0] < 0


def _cut(s, k=60):
    return s if len(s) <= k else s[:k - 1] + "…"


def render(root):
    """Each node is <div ct-g>row<div ct-k>kids</div></div>: the ct-f marker folds a node's ct-k (JS side)."""
    rows = []

    def rec(n, pre, tail):
        f, kids = n["f"], n["kids"]
        # caller line + call code, then the callee: "3행 if (x) svc.get(id) → [API 호출] GET http://.. Svc.get(..)"
        # branch conditions in red (ct-c); HTTP hops ("GET http://..", upper-case verb) in blue (ct-h)
        red = " › ".join(f'<span class="ct-c">{escape(c)}</span>' for c in n["conds"] if not re.match(r"[A-Z]+ ", c))
        blue = " › ".join(f'<span class="ct-h">[API 호출] {escape(c)}</span>' for c in n["conds"]
                          if re.match(r"[A-Z]+ ", c))
        ln, code = n["site"] or (None, None)
        call = (f'<span class="ct-l">{ln}행</span> ' if ln else "") + (red + " " if red else "") \
            + (f'{escape(_cut(code))} <span class="ct-s">→</span> ' if code else "")
        anno = escape(f.anno) + " " if f.anno and not tail else ""  # root row starts at its @XxxMapping
        tip = escape(f.name + f.params + "\n" + f"{f.file}:{f.line}")  # full path lives in the tooltip
        rows.append(f'<div class="ct-g"><div class="ct-r" data-id="{escape(f.id)}" title="{tip}">'
                    f'<span class="ct-p">{pre}{tail}</span><span class="ct-i{" ct-f" if kids else ""}"></span>'
                    + call + (blue + " " if blue else "") + anno + f'<b>{escape(_cut(f.name))}</b>{escape(_cut(f.params))} '
                    f'<span class="ct-s">{escape(f.file.rsplit("/", 1)[-1])}:{f.line}</span>'
                    + (" ↻" if n["loop"] else "") + (' <span class="ct-s">↑ 위에서 펼침</span>' if n["dup"] else "")
                    + "</div>" + ('<div class="ct-k">' if kids else ""))
        sub = pre + ("" if not tail else "   " if tail == "└─ " else "│  ")
        for i, k in enumerate(kids):
            rec(k, sub, "└─ " if i == len(kids) - 1 else "├─ ")
        rows.append("</div></div>" if kids else "</div>")

    rec(root, "", "")
    return "".join(rows)


if __name__ == "__main__":
    from types import SimpleNamespace as F
    fs = {k: F(id=k, name=f"Svc.{k}", params="(String id)", file="a/Svc.java", line=1, children=[],
               anno='@GetMapping("/x")' if k == "r" else "") for k in "rabcdeg"}
    fs["r"].children = [("a", ["if (x)"], (3, "a(id)")), ("b", [], (4, "b(id)")),
                        ("c", ["if (z)", "GET http://eqps:8080/api/eqp/{}"], (5, 'rt.get(url + "/api/eqp/" + id)'))]
    fs["a"].children = [("d", [], None), ("e", [], None), ("r", ["else (if y)"], None)]  # r: back to root -> cycle
    fs["b"].children = [("g", [], None)]
    root, cut = build(fs, "r")
    html = render(root)
    rows = re.findall(r'<div class="ct-r".*?</div>', html, re.S)
    assert html.count('class="ct-k"') == html.count("ct-f") == 3  # r, a, b have kids: foldable; leaves aren't
    assert html.count("<div") == html.count("</div>")
    assert [re.search(r'data-id="(\w)"', r)[1] for r in rows] == list("raderbgc")
    assert [re.search(r'"ct-p">([^<]*)<', r)[1] for r in rows] == \
        ["", "├─ ", "│  ├─ ", "│  ├─ ", "│  └─ ", "├─ ", "│  └─ ", "└─ "]
    assert not cut and rows[4].endswith(" ↻</div>") and build(fs, "r", cap=3)[1]
    assert '<span class="ct-l">3행</span> <span class="ct-c">if (x)</span> a(id) <span class="ct-s">→</span> <b>Svc.a'         in html and '<span class="ct-l">4행</span> b(id) <span class="ct-s">→</span> <b>Svc.b' in html
    assert ('<span class="ct-c">if (z)</span> rt.get(url + &quot;/api/eqp/&quot; + id) <span class="ct-s">→</span> '
            '<span class="ct-h">[API 호출] GET http://eqps:8080/api/eqp/{}</span> <b>Svc.c') in html
    assert '<span class="ct-c">else (if y)</span> <b>Svc.r' in rows[4]  # no call site: conditions, then callee
    assert '</span>@GetMapping(&quot;/x&quot;) <b>Svc.r</b>' in rows[0] and "@GetMapping" not in rows[4]  # root only
    g = {k: F(id=k, name=k, params="()", file="f", line=1, children=[], anno="") for k in "xyzw"}
    g["x"].children, g["y"].children = [("y", [], None), ("z", [], None)], [("w", [], None)]
    g["z"].children = [("y", [], None)]
    once = re.findall(r'<div class="ct-r".*?</div>', render(build(g, "x", float("inf"), once=True)[0]), re.S)
    assert [re.search(r'data-id="(\w)"', r)[1] for r in once] == list("xywzy") and "위에서 펼침" in once[-1]
    assert render(build(g, "x")[0]).count('class="ct-r"') == 6  # default still unfolds y twice
    print("ok")
