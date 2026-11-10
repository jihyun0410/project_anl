"""Static call-tree analyzer: tree-sitter, one generic rule set for many languages."""
import re
from dataclasses import dataclass, field

from tree_sitter_language_pack import get_parser

LANGS = {"py": "python", "java": "java", "js": "javascript", "jsx": "javascript", "mjs": "javascript",
         "ts": "typescript", "tsx": "tsx", "go": "go", "c": "c", "h": "c",
         "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "cs": "csharp"}
FUNCS = {"function_definition", "function_declaration", "generator_function_declaration", "method_definition",
         "method_declaration", "constructor_declaration", "local_function_statement",
         "arrow_function", "function_expression"}  # anonymous ones are analysed inline in their parent
CALLS = {"call", "call_expression", "method_invocation", "invocation_expression",
         "new_expression", "object_creation_expression"}
CASES = {"case_clause", "switch_case", "switch_default", "case_statement", "expression_case", "default_case",
         "type_case", "switch_section", "switch_block_statement_group", "switch_rule", "except_clause", "catch_clause"}


@dataclass
class Func:
    id: str
    name: str      # display name, e.g. Class.method
    simple: str    # bare name used for call resolution
    cls: str | None
    file: str
    lang: str
    line: int
    params: str
    code: str
    calls: list = field(default_factory=list)     # [(callee name, conds)] as written
    children: list = field(default_factory=list)  # [(Func.id, conds)] resolved


def _txt(n):
    return n.text.decode("utf-8", "replace")


def _flat(n, limit=80):
    return " ".join(_txt(n).split())[:limit]


def _last_word(s):
    w = re.findall(r"\w+", re.sub(r"<[^<>]*>", "", s))  # drop generics: ns::h<int> -> h
    return w[-1] if w else None


def _nodes(root):
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _chain(fn):
    """fn and its nested declarators: C/C++ hide name and params in int *f(...) / int& f(...)."""
    d = fn
    while d is not None:
        yield d
        d = d.child_by_field_name("declarator") or next(
            (c for c in d.named_children if c.type.endswith("declarator")), None)


def _first_field(fn, *keys):
    return next((n for d in _chain(fn) for k in keys if (n := d.child_by_field_name(k)) is not None), None)


def _name(fn):
    chain = list(_chain(fn))
    n = _first_field(fn, "name") or (chain[-1] if len(chain) > 1 else None)
    if n is None and fn.parent is not None:  # const f = () => {} / this.f = function () {}
        n = fn.parent.child_by_field_name("name") or fn.parent.child_by_field_name("left")
    return _last_word(_txt(n)) if n is not None else None


def _params(fn):
    p = _first_field(fn, "parameters", "parameter")
    t = _flat(p, 200) if p is not None else "()"
    return t if t.startswith("(") else f"({t})"


def _cls(fn):
    if (r := fn.child_by_field_name("receiver")) is not None:  # Go: func (s *S) Run()
        return _last_word(_txt(r))
    a = fn.parent
    while a is not None:
        if any(k in a.type for k in ("class", "interface", "struct", "enum", "record")) \
                and (n := a.child_by_field_name("name")) is not None:
            return _txt(n)
        a = a.parent
    return None


def _branch(p, c):
    """Condition label for descending from node p into its child c, or None."""
    if c.type in CASES:
        return re.split(r":|->|=>|\{", _flat(c), maxsplit=1)[0].strip()
    cond = p.child_by_field_name("condition")
    if cond is None and "switch" in p.type:
        cond = p.child_by_field_name("value")
    if cond is None or c == cond:
        return None
    kw = "if" if p.children[0].is_named else _txt(p.children[0])  # if / elif / while / switch / for
    if c in p.children_by_field_name("alternative"):
        return None if c.child_by_field_name("condition") is not None else f"else ({kw} {_flat(cond)})"
    return f"{kw} {_flat(cond)}"


def _calls(body, named):
    out, stack = [], [(body, [])]
    while stack:
        node, conds = stack.pop()
        for c in node.children:
            if c.id in named:  # nested named function: analysed on its own
                continue
            lab = _branch(node, c)
            cc = conds + [lab] if lab else conds
            if c.type in CALLS:
                n = next((n for k in ("function", "name", "constructor", "type")
                          if (n := c.child_by_field_name(k)) is not None), None)
                if n is not None and (callee := _last_word(_txt(n))):
                    out.append((c.start_byte, callee, cc))
            stack.append((c, cc))
    return [(callee, cc) for _, callee, cc in sorted(out, key=lambda x: x[0])]


def analyze(files):
    """files: {filename: bytes} -> (funcs by id, entry points)"""
    funcs = {}
    for fname, src in files.items():
        lang = LANGS.get(fname.rsplit(".", 1)[-1].lower())
        if not lang:
            continue
        root = get_parser(lang).parse(src).root_node
        named = {n.id: n for n in _nodes(root)
                 if n.type in FUNCS and n.child_by_field_name("body") is not None and _name(n)}
        mod = Func(f"{fname}:<module>", "<module>", "<module>", None, fname, lang, 1, "", _txt(root),
                   _calls(root, named))
        funcs[mod.id] = mod
        for n in sorted(named.values(), key=lambda n: n.start_byte):
            simple, cls, line = _name(n), _cls(n), n.start_point.row + 1
            f = Func(f"{fname}:{line}:{simple}", f"{cls}.{simple}" if cls else simple, simple, cls, fname, lang,
                     line, _params(n), _txt(n), _calls(n.child_by_field_name("body"), named))
            funcs[f.id] = f

    by_name = {}
    for f in funcs.values():
        if f.simple == "<module>":
            continue
        by_name.setdefault(f.simple, []).append(f)
        if f.simple in ("__init__", "constructor") and f.cls:  # Foo() / new Foo() -> constructor
            by_name.setdefault(f.cls, []).append(f)

    for f in funcs.values():
        seen = set()
        for callee, conds in f.calls:
            # ponytail: name-only resolution (same file wins, else every same-named def); add receiver/type
            # inference if same-named methods (get/run/...) produce false edges.
            cands = by_name.get(callee, [])
            for d in [d for d in cands if d.file == f.file] or cands:
                if (k := (d.id, tuple(conds))) not in seen:
                    seen.add(k)
                    f.children.append(k[:1] + (conds,))

    called = {cid for f in funcs.values() for cid, _ in f.children if cid != f.id}
    entries = [f for f in funcs.values() if f.id not in called and (f.simple != "<module>" or f.children)]
    return funcs, entries


if __name__ == "__main__":
    funcs, entries = analyze({
        "a.py": b"from b import helper\ndef main(x):\n    if x > 0:\n        helper(x)\n    elif x < -5:\n"
                b"        pass\n    else:\n        Svc(x).run()\nclass Svc:\n    def __init__(self, v): self.v = v\n"
                b"    def run(self): return self.v\nif __name__ == '__main__':\n    main(1)\n",
        "b.py": b"def helper(y):\n    for i in range(y):\n        helper2(i)\ndef helper2(z):\n    helper2(z)\n"
                b"def unused(): pass\n",
        "A.java": b"class A { public static void main(String[] a) { if (a.length > 0) run(1); else if (a.length < 0) "
                  b"run(2); else run(0); new A(); } A() {} static void run(int n) { switch (n) { case 1: done(); "
                  b"break; } } static void done() { try { x(); } catch (Exception e) { done(); } } }",
        "w.ts": b"export const go = (n: number) => { n > 1 ? step(n) : stop(); [1].map(v => step(v)); }\n"
                b"function step(n: number) {}\nfunction stop() {}",
        "m.go": b"package m\nfunc (s *S) Run(a int) { if a > 0 { work(a) } }\nfunc work(a int) {}",
        "x.c": b"static int g(int a){return a;}\nint *f(int a){ if (a) g(a); else g(0); return 0; }",
        "k.cpp": b"class C { public: void m(int x) { if (x) h<int>(x); } };\nint& h(int x) { static int y; return y; }",
        "k.cs": b"class K { void M(int a) { switch (a) { case 1: N(); break; } } void N() {} }",
    })
    assert {f"{e.file}:{e.name}" for e in entries} == {
        "a.py:<module>", "b.py:unused", "A.java:A.main", "w.ts:go", "m.go:S.Run", "x.c:f", "k.cpp:C.m", "k.cs:K.M"}, \
        sorted(f"{e.file}:{e.name}" for e in entries)
    tree = {f"{f.file}:{f.name}": [(funcs[c].name, conds) for c, conds in f.children] for f in funcs.values()}
    assert tree["a.py:<module>"] == [("main", ["if __name__ == '__main__'"])]
    assert tree["a.py:main"] == [("helper", ["if x > 0"]), ("Svc.run", ["else (if x > 0)"]),
                                 ("Svc.__init__", ["else (if x > 0)"])], tree["a.py:main"]
    assert tree["b.py:helper"] == [("helper2", [])]
    assert tree["A.java:A.main"] == [("A.run", ["if (a.length > 0)"]), ("A.run", ["if (a.length < 0)"]),
                                     ("A.run", ["else (if (a.length < 0))"]), ("A.A", [])], tree["A.java:A.main"]
    assert tree["A.java:A.run"] == [("A.done", ["switch (n)", "case 1"])], tree["A.java:A.run"]
    assert tree["A.java:A.done"] == [("A.done", ["catch (Exception e)"])], tree["A.java:A.done"]
    assert tree["w.ts:go"] == [("step", ["if n > 1"]), ("stop", ["else (if n > 1)"]), ("step", [])], tree["w.ts:go"]
    assert tree["m.go:S.Run"] == [("work", ["if a > 0"])]
    assert tree["x.c:f"] == [("g", ["if (a)"]), ("g", ["else (if (a))"])], tree["x.c:f"]
    assert tree["k.cpp:C.m"] == [("h", ["if (x)"])], tree["k.cpp:C.m"]
    assert tree["k.cs:K.M"] == [("K.N", ["switch a", "case 1"])], tree["k.cs:K.M"]
    assert funcs["b.py:1:helper"].params == "(y)" and funcs["x.c:2:f"].params == "(int a)"
    print("ok")
