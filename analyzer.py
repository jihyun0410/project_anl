"""Static call-tree analyzer: tree-sitter, one generic rule set for many languages.

Spring extra: outgoing HTTP calls (@Value base URL + "/path", or @FeignClient methods) are linked to the
uploaded @XxxMapping controller method whose path matches exactly. Base URLs come from application.yml.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import yaml
from tree_sitter_language_pack import get_parser

LANGS = {"py": "python", "java": "java", "js": "javascript", "jsx": "javascript", "mjs": "javascript",
         "ts": "typescript", "tsx": "tsx", "go": "go", "c": "c", "h": "c",
         "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "cs": "csharp"}
CONFIG = {"yml", "yaml"}
SKIP = {"node_modules", ".git", "target", "build", "dist", "venv", ".venv", "__pycache__", "test", "tests"}
FUNCS = {"function_definition", "function_declaration", "generator_function_declaration", "method_definition",
         "method_declaration", "constructor_declaration", "local_function_statement",
         "arrow_function", "function_expression"}  # anonymous ones are analysed inline in their parent
CALLS = {"call", "call_expression", "method_invocation", "invocation_expression",
         "new_expression", "object_creation_expression"}
CASES = {"case_clause", "switch_case", "switch_default", "case_statement", "expression_case", "default_case",
         "type_case", "switch_section", "switch_block_statement_group", "switch_rule", "except_clause", "catch_clause"}
OWNERS = ("class", "interface", "struct", "enum", "record")
VERBS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
         "PatchMapping": "PATCH", "RequestMapping": None}
VALUE_RE = re.compile(r'@Value\(\s*"([^"]*)"\s*\)\s*(?:(?:private|protected|public|final|static)\s+)*\w+\s+(\w+)')
CONST_RE = re.compile(r'\b(?:static\s+final|final\s+static)\s+String\s+(\w+)\s*=\s*"([^"]*)"')
TYPE_RE = re.compile(r"\b([A-Z]\w*)(?:<[^<>]*>)?\s+(\w+)\s*[;=,)]")  # Java-style `Type var` declarations


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
    calls: list    # [(pos, callee, receiver, conds)] as written
    http: list     # [(pos, verb, base url, path, conds)] outgoing HTTP calls
    children: list = None  # [(Func.id, conds)] resolved
    via_http: bool = False  # called over HTTP from another top-level folder (so not a first entry point)


def folder(path):
    """Top-level uploaded folder of a relative path; '' for plain file uploads."""
    return path.split("/", 1)[0] if "/" in path else ""


def keep(path):
    """Build output, dependencies and tests aren't the app's call flow (tests would hide every entry they call)."""
    return not SKIP & set(path.split("/")[:-1])


def _txt(n):
    return n.text.decode("utf-8", "replace")


def _flat(n, limit=80):
    return " ".join(_txt(n).split())[:limit]


def _words(s):
    return re.findall(r"\w+", re.sub(r"<[^<>]*>", "", s))  # drop generics: ns::h<int> -> ns, h


def _last_word(s):
    w = _words(s)
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


def _owner(fn):
    a = fn.parent
    while a is not None and not (any(k in a.type for k in OWNERS) and a.child_by_field_name("name") is not None):
        a = a.parent
    return a


def _cls(fn):
    if (r := fn.child_by_field_name("receiver")) is not None:  # Go: func (s *S) Run()
        return _last_word(_txt(r))
    return _txt(o.child_by_field_name("name")) if (o := _owner(fn)) is not None else None


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


# --- Java / Spring helpers ---------------------------------------------------------------------------------

def _is_concat(n):
    return n.type == "binary_expression" and (op := n.child_by_field_name("operator")) is not None \
        and op.type == "+"


def _leftmost(n):
    while _is_concat(n):
        n = n.child_by_field_name("left")
    return n


def _eval(n, env):
    """Java string expression -> str; parts unknown statically become {} (a path wildcard)."""
    if _is_concat(n):
        return _eval(n.child_by_field_name("left"), env) + _eval(n.child_by_field_name("right"), env)
    if n.type == "string_literal":
        return _txt(n)[1:-1]
    if n.type == "parenthesized_expression":
        return _eval(n.named_children[0], env)
    return env.get(_last_word(_txt(n)), "{}")


def _verb(n):
    """HTTP verb from the statement holding a URL expression (getForObject / .post() / HttpMethod.PUT)."""
    while n.parent is not None and not n.type.endswith(("statement", "declaration")):
        n = n.parent
    t = _txt(n)
    m = re.search(r"HttpMethod\.(\w+)", t) or re.search(r"\b(get|post|put|delete|patch)(?:For\w+)?\s*\(", t, re.I)
    return m[1].upper() if m else None


def _annos(decl):
    """Java annotations on a declaration -> {name: {attr: value node}}."""
    out = {}
    for m in decl.children if decl is not None else []:
        for a in m.named_children if m.type == "modifiers" else []:
            if a.type in ("annotation", "marker_annotation"):
                args = a.child_by_field_name("arguments")
                out[_last_word(_txt(a.child_by_field_name("name")))] = {
                    (_txt(x.child_by_field_name("key")) if x.type == "element_value_pair" else "value"):
                    (x.child_by_field_name("value") if x.type == "element_value_pair" else x)
                    for x in (args.named_children if args is not None else [])}
    return out


def _mapping(annos, env):
    """(verb, [paths]) from a Spring @XxxMapping, or None."""
    for name, verb in VERBS.items():
        if (kv := annos.get(name)) is None:
            continue
        v = kv.get("value", kv.get("path"))
        vals = v.named_children if v is not None and v.type == "element_value_array_initializer" else \
            [v] if v is not None else []
        if verb is None and "method" in kv:
            verb = _last_word(_txt(kv["method"]))  # RequestMethod.POST
        return verb, [_eval(x, env) for x in vals] or [""]
    return None


def _spring(fn, env, prop):
    """Java method -> (controller endpoints [(verb, path)], Feign HTTP calls [(verb, base, path)])."""
    m, owner = _mapping(_annos(fn), env), _owner(fn)
    if m is None or owner is None:
        return [], []
    oa = _annos(owner)
    verb, paths = m
    full = [p + q for p in (_mapping(oa, env) or (None, [""]))[1] for q in paths]
    if (fc := oa.get("FeignClient")) is None:
        return [(verb, p) for p in full], []
    base = prop(_eval(fc["url"], env)) if "url" in fc else ""
    pre = _eval(fc["path"], env) if "path" in fc else ""
    return [], [(verb, base, pre + p) for p in full]


def _segs(path):
    return [s for s in re.sub(r"\{[^}]*\}", "{}", path.split("?")[0]).split("/") if s]


def _same(call, endpoint):
    """Exact path match. Endpoint {var} takes any segment; a dynamic call segment only fits an endpoint {var}."""
    a, b = _segs(call), _segs(endpoint)
    return len(a) == len(b) and all(x == y or y == "{}" for x, y in zip(a, b))


def _flatten(d, pre=""):
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        out.update(_flatten(v, f"{pre}{k}.") if isinstance(v, dict) else {f"{pre}{k}": str(v)})
    return out


# --- analysis ----------------------------------------------------------------------------------------------

def _calls(body, named, env, urls):
    calls, http, stack = [], [], [(body, [])] if body is not None else []
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
                obj = c.child_by_field_name("object")  # Java: obj.name(...)
                w = _words((_txt(obj) + "." if obj is not None else "") + _txt(n)) if n is not None else []
                if w:
                    calls.append((c.start_byte, w[-1], w[-2] if len(w) > 1 else None, cc))
            elif urls and _is_concat(c) and not _is_concat(node) \
                    and (base := urls.get(_last_word(_txt(_leftmost(c))))) is not None:
                http.append((c.start_byte, _verb(c), base, _eval(c, env), cc))  # eqpsUrl + "/api/x"
            stack.append((c, cc))
    return calls, http


def analyze(files):
    """files: {relative path: bytes} -> (funcs by id, entry points)"""
    files = {k: v for k, v in files.items() if keep(k)}
    yml = {}
    for fname, src in files.items():
        # only Spring config: a folder upload also brings docker-compose / helm yml that may not even parse
        if fname.rsplit(".", 1)[-1].lower() in CONFIG and fname.rsplit("/", 1)[-1].startswith("application"):
            for doc in yaml.safe_load_all(src):
                for k, v in _flatten(doc).items():
                    yml.setdefault(k, v)  # ponytail: first yml document (default profile) wins

    def prop(s):  # ${key:default} -> value; twice, since yml values may hold placeholders themselves
        for _ in range(2):
            s = re.sub(r"\$\{([^}:]+)(?::([^}]*))?\}", lambda m: yml.get(m[1], m[2] or ""), s)
        return s

    texts = {f: s.decode("utf-8", "replace") for f, s in files.items() if f.rsplit(".", 1)[-1].lower() in LANGS}
    consts = {k: v for t in texts.values() for k, v in CONST_RE.findall(t)}
    funcs, endpoints, types = {}, [], {}
    for fname, text in texts.items():
        lang = LANGS[fname.rsplit(".", 1)[-1].lower()]
        urls = {var: prop(p) for p, var in VALUE_RE.findall(text)}  # @Value("${eqps.service-url}") String var
        for field, var in re.findall(r"this\.(\w+)\s*=\s*(\w+)\s*;", text):  # constructor-injected @Value
            if var in urls:
                urls[field] = urls[var]
        env = consts | dict.fromkeys(urls, "")
        types[fname] = {var: t for t, var in TYPE_RE.findall(text)}
        root = get_parser(lang).parse(files[fname]).root_node
        named = {n.id: n for n in _nodes(root) if n.type in FUNCS and _name(n) and (
            n.child_by_field_name("body") is not None or "FeignClient" in _annos(_owner(n)))}
        mod = Func(f"{fname}:<module>", "<module>", "<module>", None, fname, lang, 1, "", text,
                   *_calls(root, named, env, urls))
        funcs[mod.id] = mod
        for n in sorted(named.values(), key=lambda n: n.start_byte):
            simple, cls, line = _name(n), _cls(n), n.start_point.row + 1
            f = Func(f"{fname}:{line}:{simple}", f"{cls}.{simple}" if cls else simple, simple, cls, fname, lang,
                     line, _params(n), _txt(n), *_calls(n.child_by_field_name("body"), named, env, urls))
            if lang == "java":
                eps, feign = _spring(n, env, prop)
                endpoints += [(f, verb, path) for verb, path in eps]
                f.http += [(0, verb, base, path, []) for verb, base, path in feign]
            funcs[f.id] = f

    by_name = {}
    for f in funcs.values():
        if f.simple == "<module>":
            continue
        by_name.setdefault(f.simple, []).append(f)
        if f.simple in ("__init__", "constructor") and f.cls:  # Foo() / new Foo() -> constructor
            by_name.setdefault(f.cls, []).append(f)

    called = set()
    for f in funcs.values():
        edges = []
        for pos, callee, recv, conds in f.calls:
            cands, t = by_name.get(callee, []), types[f.file].get(recv)
            # ponytail: name-based resolution, tiers: receiver's declared type (Foo / FooImpl) > same file >
            # every same-named def. Add real type inference if false edges still show up.
            for d in [d for d in cands if t and d.cls in (t, t + "Impl")] or \
                    [d for d in cands if d.file == f.file] or cands:
                edges.append((pos, d, conds))
                if d is not f:
                    called.add(d.id)
        for pos, verb, base, path, conds in f.http:
            # service-url's own path (e.g. /eqps) may be the target's context-path or part of its mapping
            want = (path, urlparse(base).path + path)
            for d, everb, epath in endpoints:  # HTTP targets stay out of `called`: controllers remain entries
                if (verb is None or everb is None or verb == everb) and any(_same(w, epath) for w in want):
                    edges.append((pos, d, conds + [f"{verb or 'HTTP'} {base}{path}"]))
                    d.via_http |= folder(d.file) != folder(f.file)
        f.children, seen = [], set()
        for _, d, conds in sorted(edges, key=lambda e: e[0]):
            if (k := (d.id, tuple(conds))) not in seen:
                seen.add(k)
                f.children.append(k[:1] + (conds,))

    entries = [f for f in funcs.values() if f.id not in called and (f.simple != "<module>" or f.children)
               and not f.file.endswith("RPL.java")]  # *RPL.java: never listed as entries, still shown in trees
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
        # Spring: caller service (yml + @Value + Feign) -> controllers of the eqps service
        "application.yml": b"eqps:\n  service-url: ${EQPS_URL:http://eqps:8080/eqps}\n",
        "EqpService.java": b"""@Service public class EqpService {
            @Value("${eqps.service-url}") private String eqpsUrl;
            private static final String EQP_API = "/api/eqp";
            private final EqpsClient eqpsClient;
            public Eqp get(String id, boolean feign) {
                if (feign) return eqpsClient.find(id);
                return restTemplate.getForObject(eqpsUrl + EQP_API + "/" + id, Eqp.class);
            }
            public void save(Eqp e) { restTemplate.postForObject(eqpsUrl + "/api/eqp", e, Void.class); }
            public void ping() { String u = eqpsUrl + "/health"; restTemplate.getForObject(u, String.class); }
        }""",
        "EqpsClient.java": b"""@FeignClient(name = "eqps", url = "${eqps.service-url}") @RequestMapping("/api")
        public interface EqpsClient { @GetMapping("/eqp/{id}") Eqp find(@PathVariable("id") String id); }""",
        "EqpController.java": b"""@RestController @RequestMapping("/api/eqp") public class EqpController {
            @GetMapping("/{id}") public Eqp find(@PathVariable String id) { return repo(id); }
            @PostMapping public void create(@RequestBody Eqp e) {}
            @RequestMapping(value = {"/list", "/all"}, method = RequestMethod.GET) public List<Eqp> list() { return null; }
            private Eqp repo(String id) { return null; }
        }""",
        "Health.java": b'@RestController class Health { @GetMapping("/eqps/health") String health() { return "ok"; } }',
        # folder upload noise: ignored, or EqpService.get would stop being an entry / yaml would fail to parse
        "svc/src/test/java/EqpServiceTest.java": b"class T { @Test void t() { new EqpService().get(\"1\", true); } }",
        "svc/node_modules/x/index.js": b"function get() {}",
        "helm/templates/deploy.yaml": b"{{ .Values.x }}: [",
    })
    assert {f"{e.file}:{e.name}" for e in entries} == {
        "a.py:<module>", "b.py:unused", "A.java:A.main", "w.ts:go", "m.go:S.Run", "x.c:f", "k.cpp:C.m", "k.cs:K.M",
        "EqpService.java:EqpService.get", "EqpService.java:EqpService.save", "EqpService.java:EqpService.ping",
        "EqpController.java:EqpController.find", "EqpController.java:EqpController.create",
        "EqpController.java:EqpController.list", "Health.java:Health.health"}, \
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
    base = "http://eqps:8080/eqps"
    assert tree["EqpService.java:EqpService.get"] == [
        ("EqpsClient.find", ["if (feign)"]),
        ("EqpController.find", [f"GET {base}/api/eqp/{{}}"])], tree["EqpService.java:EqpService.get"]
    assert tree["EqpsClient.java:EqpsClient.find"] == [("EqpController.find", [f"GET {base}/api/eqp/{{id}}"])]
    assert tree["EqpService.java:EqpService.save"] == [("EqpController.create", [f"POST {base}/api/eqp"])]
    assert tree["EqpService.java:EqpService.ping"] == [("Health.health", [f"HTTP {base}/health"])]
    assert tree["EqpController.java:EqpController.find"] == [("EqpController.repo", [])]
    assert not any(f.via_http for f in funcs.values())  # all in one folder: HTTP targets stay first entries
    funcs, entries = analyze({  # two mapped service folders: the HTTP-called controller is no first entry point
        "caller/src/S.java": b'class S { @Value("${u}") String u; void a() { rt.getForObject(u + "/x", String.class); } }',
        "eqps/src/C.java": b'@RestController class C { @GetMapping("/x") String x() { return ""; } }',
        "eqps/src/EqpRPL.java": b"class EqpRPL { void handle() { done(); } void done() {} }",  # uncalled, but RPL
    })
    assert {e.name for e in entries} == {"S.a", "C.x"} and [e.name for e in entries if not e.via_http] == ["S.a"]
    assert [funcs[c].name for c, _ in funcs["eqps/src/EqpRPL.java:1:handle"].children] == ["EqpRPL.done"]
    assert (folder("caller/src/S.java"), folder("S.java"), keep("a/src/test/T.java"), keep("a/src/T.java")) == \
        ("caller", "", False, True)
    print("ok")
