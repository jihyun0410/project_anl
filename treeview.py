"""Indented call tree (├─ └─) as HTML rows. A branch (if / else / case / loop) is a row of its own, in red with its
line, and the calls made inside it hang below it; every call row is clickable via data-id."""
import re
from html import escape

HTTP = re.compile(r"[A-Z]+ ")  # inline label "GET http://.." = HTTP hop (blue); others, e.g. "[구현체 2개 중]", red
CELL = {"│": "ct-v", "├": "ct-j", "└": "ct-e", " ": "ct-0"}  # tree guide cell -> CSS that draws its lines


def _cells(glyphs):
    """'│  ├─ ' -> one 3-char cell per level. CSS draws the guides (the glyphs stay as invisible text), so a
    wrapped row keeps its │ lines down to the next row."""
    return "".join(f'<span class="{CELL[glyphs[i]]}">{glyphs[i:i + 3]}</span>' for i in range(0, len(glyphs), 3))


def build(funcs, fid, cap=300, once=False, skip=frozenset()):
    """Unfold the call graph from fid into a tree; cycles are cut, at most cap call nodes. -> (root, truncated)
    once: a func with calls unfolds only at its first row, later rows point up to it (size ~ call edges, no blowup)
    skip: files whose funcs are left out, together with everything they call
    A call's branch path (its (label, line) conds) becomes nested branch nodes; consecutive calls inside the same
    branch share one node, so `if (x) { a(); b(); }` shows `if (x)` once with a and b under it."""
    left, seen = [cap], set()

    def rec(fid, inline, site, path):
        loop = fid in path
        dup = once and not loop and fid in seen and bool(funcs[fid].children)
        seen.add(fid)
        n = {"f": funcs[fid], "conds": inline, "site": site, "loop": loop, "dup": dup, "kids": []}
        opened = []  # [(branch, branch node)] open right now, outermost first
        for cid, cc, cs in [] if loop or dup else funcs[fid].children:
            if funcs[cid].file in skip:
                continue
            left[0] -= 1
            if left[0] < 0:
                break
            branch = [c for c in cc if isinstance(c, tuple)]
            k = 0
            while k < min(len(opened), len(branch)) and opened[k][0] == branch[k]:
                k += 1
            del opened[k:]
            for b in branch[k:]:
                node = {"cond": b, "kids": []}
                (opened[-1][1] if opened else n)["kids"].append(node)
                opened.append((b, node))
            (opened[-1][1] if opened else n)["kids"].append(
                rec(cid, [c for c in cc if not isinstance(c, tuple)], cs, path | {fid}))
        return n

    return rec(fid, [], None, frozenset()), left[0] < 0


def render(root):
    """Each node is <div ct-g>row<div ct-k>kids</div></div>: the ct-f marker folds a node's ct-k (JS side).
    A row is prefix + fold marker + ct-t text; nothing is cut, long text wraps inside ct-t under its own indent."""
    rows = []

    def rec(n, pre, tail):
        kids = n["kids"]
        if "cond" in n:  # branch row: line + condition, the calls inside it below
            label, ln = n["cond"]
            row = (f'<div class="ct-r ct-b"><span class="ct-p">{_cells(pre + tail)}</span>'
                   '<span class="ct-i ct-f"></span>'
                   f'<span class="ct-t"><span class="ct-l">{ln}행</span> <span class="ct-c">{escape(label)}</span>'
                   '</span></div>')
        else:
            # caller line + call code, then the callee: "3행 svc.get(id) → [API 호출] GET http://.. Svc.get(..)"
            f = n["f"]
            red = " › ".join(f'<span class="ct-c">{escape(c)}</span>' for c in n["conds"] if not HTTP.match(c))
            blue = " › ".join(f'<span class="ct-h">[API 호출] {escape(c)}</span>' for c in n["conds"]
                              if HTTP.match(c))
            ln, code = n["site"] or (None, None)
            call = (f'<span class="ct-l">{ln}행</span> ' if ln else "") + (red + " " if red else "") \
                + (f'{escape(code)} <span class="ct-s">→</span> ' if code else "")
            anno = escape(f.anno) + " " if f.anno and not tail else ""  # root row starts at its @XxxMapping
            tip = escape(f.name + f.params + "\n" + f"{f.file}:{f.line}")  # full path lives in the tooltip
            row = (f'<div class="ct-r" data-id="{escape(f.id)}" title="{tip}">'
                   f'<span class="ct-p">{_cells(pre + tail)}</span><span class="ct-i{" ct-f" if kids else ""}"></span>'
                   '<span class="ct-t">' + call + (blue + " " if blue else "") + anno
                   + f'<b>{escape(f.name)}</b>{escape(f.params)} '
                   f'<span class="ct-s">{escape(f.file.rsplit("/", 1)[-1])}:{f.line}</span>'
                   + (" ↻" if n["loop"] else "") + (' <span class="ct-s">↑ 위에서 펼침</span>' if n["dup"] else "")
                   + (f'<span class="ct-hide" data-file="{escape(f.file)}">✕ 파일 숨기기</span>' if tail else "")
                   + "</span></div>")
        rows.append('<div class="ct-g">' + row + ('<div class="ct-k">' if kids else ""))
        sub = pre + ("" if not tail else "   " if tail == "└─ " else "│  ")
        for i, k in enumerate(kids):
            rec(k, sub, "└─ " if i == len(kids) - 1 else "├─ ")
        rows.append("</div></div>" if kids else "</div>")

    rec(root, "", "")
    return "".join(rows)


if __name__ == "__main__":
    from types import SimpleNamespace as F

    def outline(html):  # [(tree prefix, call id or branch label)] per row
        return [(re.sub(r"<[^>]+>", "", re.search(r'"ct-p">(.*?)</span><span class="ct-i', r)[1]),
                 (re.search(r'data-id="(\w)"', r) or
                 re.search(r'class="ct-c">([^<]*)<', r))[1]) for r in re.findall(r'<div class="ct-r.*?</div>', html, re.S)]

    fs = {k: F(id=k, name=f"Svc.{k}", params="(String id)", file="a/Svc.java", line=1, children=[],
               anno='@GetMapping("/x")' if k == "r" else "") for k in "rabcdeg"}
    fs["r"].children = [("a", [("if (x)", 3)], (3, "a(id)")), ("b", [("if (x)", 3)], (4, "b(id)")),
                        ("c", [("if (z)", 5), "GET http://eqps:8080/api/eqp/{}"], (6, 'rt.get(url + "/api/eqp/" + id)'))]
    fs["a"].children = [("d", [], None), ("e", [], None), ("r", [("else", 9)], None)]  # r: back to root -> cycle
    fs["b"].children = [("g", [], None)]
    root, cut = build(fs, "r")
    html = render(root)
    assert outline(html) == [  # a and b share one `if (x)` row; each branch's calls sit one level below it
        ("", "r"), ("├─ ", "if (x)"), ("│  ├─ ", "a"), ("│  │  ├─ ", "d"), ("│  │  ├─ ", "e"),
        ("│  │  └─ ", "else"), ("│  │     └─ ", "r"), ("│  └─ ", "b"), ("│     └─ ", "g"), ("└─ ", "if (z)"),
        ("   └─ ", "c")], outline(html)
    assert html.count('class="ct-k"') == html.count("ct-f") == 6  # r, a, b and the 3 branches fold; leaves don't
    assert html.count("<div") == html.count("</div>") and html.count('class="ct-hide"') == 7  # call rows but root
    assert '<span class="ct-t"><span class="ct-l">3행</span> <span class="ct-c">if (x)</span></span></div>' in html
    assert '<span class="ct-l">3행</span> a(id) <span class="ct-s">→</span> <b>Svc.a' in html  # call row below it
    assert ('<span class="ct-l">6행</span> rt.get(url + &quot;/api/eqp/&quot; + id) <span class="ct-s">→</span> '
            '<span class="ct-h">[API 호출] GET http://eqps:8080/api/eqp/{}</span> <b>Svc.c') in html
    assert not cut and build(fs, "r", cap=3)[1] and "@GetMapping(&quot;/x&quot;) <b>Svc.r</b>" in html
    assert html.count("@GetMapping") == 1 and ' ↻<span class="ct-hide"' in html  # annotation on the root only
    long = "(String alpha, String beta, String gamma, String delta, String epsilon, String zeta, String eta)"
    fs["g"].params, fs["b"].children = long, [("g", [("if (" + "x != 1 && " * 12 + "y)", 4)], None)]
    html = render(build(fs, "r")[0])
    assert long in html and "if (" + "x != 1 &amp;&amp; " * 12 + "y)" in html and "…" not in html  # nothing cut
    h = {k: F(id=k, name=k, params="()", file="f", line=1, children=[], anno="") for k in "xyz"}
    h["x"].children = [("y", [("else", 3)], None), ("z", [("else", 7)], None)]  # two separate else blocks
    assert outline(render(build(h, "x")[0])) == [("", "x"), ("├─ ", "else"), ("│  └─ ", "y"), ("└─ ", "else"),
                                                ("   └─ ", "z")]
    g = {k: F(id=k, name=k, params="()", file="f", line=1, children=[], anno="") for k in "xyzw"}
    g["x"].children, g["y"].children = [("y", [], None), ("z", [], None)], [("w", [], None)]
    g["z"].children = [("y", [], None)]
    once = re.findall(r'<div class="ct-r".*?</div>', render(build(g, "x", float("inf"), once=True)[0]), re.S)
    assert [re.search(r'data-id="(\w)"', r)[1] for r in once] == list("xywzy") and "위에서 펼침" in once[-1]
    assert render(build(g, "x")[0]).count('class="ct-r"') == 6  # default still unfolds y twice
    g["w"].file = "util/W.java"  # skipped file: its row and its subtree are gone, the rest stays
    assert re.findall(r'data-id="(\w)"', render(build(g, "x", skip={"util/W.java"})[0])) == list("xyzy")
    print("ok")
