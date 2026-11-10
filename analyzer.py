"""Static call-tree analyzer: tree-sitter, one generic rule set for many languages.

Spring extra: outgoing HTTP calls (@Value base URL + "/path", or @FeignClient methods) are linked to the
uploaded @XxxMapping controller method whose path matches exactly. Base URLs come from application.yml.
Calls on injected fields follow Spring DI: interface -> bean implementation (extends / implements, @Component
family + @Bean methods), narrowed by @Qualifier > @Primary > field name like Spring does.
Python (UI) HTTP calls - requests / httpx (+ Session / Client objects) and wrappers like api_get("/x") or
call_api("DELETE", f"{BASE}/x/{id}") - are linked to the same controller endpoints; their base URL is a wildcard.
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
QUAL_RE = re.compile(r'@(?:Qualifier\(\s*(?:value\s*=\s*)?|Resource\(\s*name\s*=\s*)"([^"]+)"\s*\)\s*'
                     r'(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|protected|public|final|static)\s+)*'
                     r'[A-Z]\w*(?:<[^<>]*>)?\s+(\w+)')  # @Qualifier("x") [final] Type var  (field / ctor / setter param)
HTTP_UP = {v for v in VERBS.values() if v}  # GET POST PUT DELETE PATCH
PY_HTTP_LIBS = ("requests", "httpx")
SESSION_RE = re.compile(r"(\w+)\s*=\s*(?:requests|httpx)\.(?:Session|Client|AsyncClient)\(|"
                        r"(?:requests|httpx)\.(?:Session|Client|AsyncClient)\([^)]*\)\s+as\s+(\w+)")
FAMILY = {"typescript": "javascript", "tsx": "javascript", "cpp": "c"}  # name-based calls stay inside a family
JCLASSES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
STEREO = ("Component", "Service", "Repository", "Controller", "RestController", "Configuration")


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
    calls: list    # [(pos, callee, receiver, conds, site)] as written; site = (file row, call code)
    http: list     # [(pos, verb, base url, path, conds, site | None)] outgoing HTTP calls (Feign: no site)
    children: list = None  # [(Func.id, conds, (file line of the call, call code) | None)] resolved, one per call
    via_http: bool = False  # called over HTTP from another top-level folder (so not a first entry point)
    anno: str = ""  # Spring mapping annotation as written, e.g. @GetMapping("/{id}")


def folder(path):
    """Top-level uploaded folder of a relative path; '' for plain file uploads."""
    return path.split("/", 1)[0] if "/" in path else ""


def keep(path):
    """Build output, dependencies and tests aren't the app's call flow (tests would hide every entry they call)."""
    return not SKIP & set(path.split("/")[:-1])


def _txt(n):
    return n.text.decode("utf-8", "replace")


def _flat(n):  # whole text on one line: long conditions / params are wrapped by the tree, never cut
    return " ".join(_txt(n).split())


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
    t = _flat(p) if p is not None else "()"
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
    """(label, line) of the branch entered going from node p into its child c, or None.
    The line tells two same-looking branches apart (two separate `else` blocks) and is shown on the branch row."""
    if c.type in CASES:
        return re.split(r":|->|=>|\{", _flat(c), maxsplit=1)[0].strip(), c.start_point.row + 1
    cond = p.child_by_field_name("condition")
    if cond is None and "switch" in p.type:
        cond = p.child_by_field_name("value")
    if cond is None or c == cond:
        return None
    kw = "if" if p.children[0].is_named else _txt(p.children[0])  # if / elif / while / switch / for
    if c in p.children_by_field_name("alternative"):  # else-if / elif: the nested if labels its own branch
        return None if c.child_by_field_name("condition") is not None else ("else", c.start_point.row + 1)
    if kw == "if" and p.parent is not None and p in p.parent.children_by_field_name("alternative"):
        kw = "else if"  # if (a) {..} else if (b) {..}
    return f"{kw} {_flat(cond)}", p.start_point.row + 1


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


def _site(c):
    """(0-based row, call code) of a call; a URL concat reports the call it's passed to, if any."""
    p = c
    while p is not None and p.type not in CALLS and p.type not in FUNCS:
        p = p.parent
    n = p if p is not None and p.type in CALLS else c
    return n.start_point.row, " ".join(_txt(n).split()).replace("( ", "(").replace(" )", ")")  # multi-line calls


def _py_url(n):
    """Python URL expression -> template, dynamic parts as {}: f"{B}/u/{id}", B + "/u", "{}/u".format(..), "/u/%s" % x."""
    t = n.type
    if t == "string":
        return "".join(_txt(c) if c.type == "string_content" else "{}" if c.type == "interpolation" else ""
                       for c in n.children)
    if t == "concatenated_string":
        return "".join(map(_py_url, n.named_children))
    if t == "parenthesized_expression" and n.named_children:
        return _py_url(n.named_children[0])
    if t == "binary_operator":
        op, left, right = (_txt(n.child_by_field_name("operator")), n.child_by_field_name("left"),
                           n.child_by_field_name("right"))
        if op == "+":
            return _py_url(left) + _py_url(right)
        if op == "%":
            return re.sub(r"%(?:\([^)]*\))?[sdifr]", "{}", _py_url(left))
    if t == "call" and (f := n.child_by_field_name("function")) is not None and f.type == "attribute" \
            and _txt(f.child_by_field_name("attribute")) == "format":
        return _py_url(f.child_by_field_name("object"))
    return "{}"


def _py_path(tmpl):
    """URL template -> path; the base URL is a wildcard: {}/api/x -> /api/x, http://h:80/api/x -> /api/x."""
    t = tmpl.split("?")[0].split("#")[0]
    t = "/" + t.split("://", 1)[1].partition("/")[2] if "://" in t else t.lstrip("{}")
    return t if len(t) > 1 and t.startswith("/") else None


def _py_http(c, sessions):
    """Python call -> (VERB | None, path, is requests / httpx itself) when it sends an HTTP request, else None.
    Verb: a "GET"-like string arg / method=, else a verb in the function name (get, api_post, deleteUser)."""
    fn, args = c.child_by_field_name("function"), c.child_by_field_name("arguments")
    w = _words(_txt(fn)) if fn is not None else []
    if not w or args is None or args.type != "argument_list":
        return None
    lib = w[0] in PY_HTTP_LIBS or len(w) > 1 and w[-2] in sessions
    pos = [a for a in args.named_children if a.type != "keyword_argument"]
    kw = {_txt(a.child_by_field_name("name")): a.child_by_field_name("value")
          for a in args.named_children if a.type == "keyword_argument"}

    def verb_of(a):
        v = _py_url(a).upper() if a is not None and a.type == "string" else ""
        return v if v in HTTP_UP else None

    i = next((i for i, a in enumerate(pos) if verb_of(a)), None)
    if verb_of(kw.get("method")):  # requests.request(method="PUT", url=...)
        verb, url = verb_of(kw["method"]), kw.get("url") or (pos[0] if pos else None)
    elif i is not None:  # requests.request("PUT", url) / call_api("DELETE", url)
        verb, url = verb_of(pos[i]), kw.get("url") or (pos[i + 1] if i + 1 < len(pos) else None)
    else:  # requests.get(url) / api_get(url) / fetch(url): no verb -> any
        parts = [x.upper() for x in re.findall(r"[A-Za-z][a-z]*", w[-1])]
        verb, url = next((x for x in parts if x in HTTP_UP), None), kw.get("url") or (pos[0] if pos else None)
    path = _py_path(_py_url(url)) if url is not None else None
    return (verb, path, lib) if path else None


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


def _supers(cls):
    """Java class / interface node -> simple names it extends or implements."""
    out = []
    for c in cls.children:
        if c.type in ("superclass", "super_interfaces", "extends_interfaces"):
            s = _txt(c)
            for _ in range(5):  # nested generics: Other<Map<String, Integer>> -> Other
                s = re.sub(r"<[^<>]*>", "", s)
            out += [_last_word(x) for x in s.split(None, 1)[1].split(",")]
    return out


def _strs(kv, *keys):
    """String literals of the first present annotation attribute: @Service("x"), @Bean(name = {"a", "b"})."""
    v = next((kv[k] for k in keys if k in kv), None)
    return re.findall(r'"([^"]*)"', _txt(v)) if v is not None else []


def _bean_name(cls):  # Spring's default bean name: FooService -> fooService, URLService stays
    return cls if cls[1:2].isupper() else cls[:1].lower() + cls[1:]


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

def _calls(body, named, env, urls, sessions=()):
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
                h = _py_http(c, sessions) if c.type == "call" else None  # Python
                if h:
                    http.append((c.start_byte, h[0], "", h[1], cc, _site(c)))
                if w and not (h and h[2]):  # requests.get(...) is the HTTP hop itself, not a call into the project
                    calls.append((c.start_byte, w[-1], w[-2] if len(w) > 1 else None, cc, _site(c)))
            elif urls and _is_concat(c) and not _is_concat(node) \
                    and (base := urls.get(_last_word(_txt(_leftmost(c))))) is not None:
                http.append((c.start_byte, _verb(c), base, _eval(c, env), cc, _site(c)))  # eqpsUrl + "/api/x"
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
    funcs, endpoints, types, quals = {}, [], {}, {}
    parents, concrete, beans = {}, set(), {}  # Java: class -> supertypes, instantiable classes, bean -> [names, primary]

    def bean(c, names, primary):
        b = beans.setdefault(c, [set(), False])
        b[0].update(names)
        b[1] |= primary

    for fname, text in texts.items():
        lang = LANGS[fname.rsplit(".", 1)[-1].lower()]
        urls = {var: prop(p) for p, var in VALUE_RE.findall(text)}  # @Value("${eqps.service-url}") String var
        quals[fname] = {var: q for q, var in QUAL_RE.findall(text)}
        for field, var in re.findall(r"this\.(\w+)\s*=\s*(\w+)\s*;", text):  # constructor / setter injection
            for m in (urls, quals[fname]):
                if var in m:
                    m[field] = m[var]
        env = consts | dict.fromkeys(urls, "")
        sessions = {v for g in SESSION_RE.findall(text) for v in g if v}  # s = requests.Session()
        types[fname] = {var: t for t, var in TYPE_RE.findall(text)}
        root = get_parser(lang).parse(files[fname]).root_node
        for n in _nodes(root) if lang == "java" else ():
            if n.type in JCLASSES and (nm := n.child_by_field_name("name")) is not None:
                c, a = _txt(nm), _annos(n)
                parents[c] = _supers(n)
                if n.type != "interface_declaration" and "abstract" not in \
                        next((_txt(m).split() for m in n.children if m.type == "modifiers"), []):
                    concrete.add(c)
                if any(k in a for k in STEREO):
                    bean(c, [x for k in STEREO if k in a for x in _strs(a[k], "value")] or [_bean_name(c)],
                         "Primary" in a)
                    bean(c, _strs(a.get("Qualifier", {}), "value"), False)
        named = {n.id: n for n in _nodes(root) if n.type in FUNCS and _name(n) and (
            n.child_by_field_name("body") is not None or "FeignClient" in _annos(_owner(n)))}
        mod = Func(f"{fname}:<module>", "<module>", "<module>", None, fname, lang, 1, "", text,
                   *_calls(root, named, env, urls, sessions))
        funcs[mod.id] = mod
        for n in sorted(named.values(), key=lambda n: n.start_byte):
            simple, cls, line = _name(n), _cls(n), n.start_point.row + 1
            fid = f"{fname}:{line}:{simple}"
            fid += f":{n.start_point.column}" if fid in funcs else ""  # same name twice on one line: A.m() {} B.m() {}
            f = Func(fid, f"{cls}.{simple}" if cls else simple, simple, cls, fname, lang,
                     line, _params(n), _txt(n), *_calls(n.child_by_field_name("body"), named, env, urls, sessions))
            if lang == "java":
                eps, feign = _spring(n, env, prop)
                endpoints += [(f, verb, path) for verb, path in eps]
                f.http += [(0, verb, base, path, [], None) for verb, base, path in feign]
                f.anno = next((" ".join(_txt(a).split()) for m in n.children if m.type == "modifiers"
                               for a in m.named_children if a.type in ("annotation", "marker_annotation")
                               and _last_word(_txt(a.child_by_field_name("name"))) in VERBS), "")
                if "Bean" in (ba := _annos(n)):  # @Bean method: its `return new X()` (else return type) is a bean
                    rt, new = n.child_by_field_name("type"), re.search(r"\breturn\s+new\s+([A-Z]\w*)", f.code)
                    bean(new[1] if new else _last_word(_txt(rt)) if rt is not None else simple,
                         (_strs(ba["Bean"], "value", "name") or [simple]) + _strs(ba.get("Qualifier", {}), "value"),
                         "Primary" in ba)
            funcs[f.id] = f

    by_name = {}
    for f in funcs.values():
        if f.simple == "<module>":
            continue
        by_name.setdefault(f.simple, []).append(f)
        if f.simple in ("__init__", "constructor") and f.cls:  # Foo() / new Foo() -> constructor
            by_name.setdefault(f.cls, []).append(f)

    subs = {}  # type -> instantiable classes that are it or extend / implement it, transitively
    for c in concrete:
        seen, todo = set(), [c]
        while todo:
            if (x := todo.pop()) not in seen:
                seen.add(x)
                todo += parents.get(x, [])
        for x in seen:
            subs.setdefault(x, set()).add(c)

    def inject(t, var, file):
        """Classes a `t`-typed injection point named var resolves to: beans first, then Qualifier > Primary > name."""
        cs = subs.get(t) or ({t} if t in parents else set())
        cs = {c for c in cs if c in beans} or cs  # non-bean implementations (test doubles etc.) only as a fallback
        if len(cs) > 1:
            q, names = quals[file].get(var), lambda c: beans.get(c, [set()])[0]
            cs = (q and {c for c in cs if q in names(c)}) or {c for c in cs if beans.get(c, [0, False])[1]} \
                or {c for c in cs if var in names(c)} or cs
        return cs

    def lookup(c, name):  # c's own `name` methods, else the nearest inherited ones
        level, seen = {c}, set()
        while level:
            if hit := [d for d in by_name.get(name, []) if d.lang == "java" and d.cls in level]:
                return hit
            seen |= level
            level = {p for x in level for p in parents.get(x, [])} - seen
        return []

    called = set()
    for f in funcs.values():
        edges = []
        rel = lambda site: site and (site[0] + 1, site[1])  # 0-based row -> file line number
        for pos, callee, recv, conds, site in f.calls:
            fam = FAMILY.get(f.lang, f.lang)
            cands = [d for d in by_name.get(callee, []) if FAMILY.get(d.lang, d.lang) == fam]
            t, hits, mark = types[f.file].get(recv), [], []
            if f.lang == "java" and f.cls:  # this / super / injected field / static Class.call()
                start = {f.cls} if recv in (None, "this") else set(parents.get(f.cls, [])) if recv == "super" \
                    else inject(t, recv, f.file) if t else {recv} if recv in parents else set()
                hits = list({d.id: d for c in sorted(start) for d in lookup(c, callee)}.values())
                if len({d.cls for d in hits}) > 1:
                    mark = [f"[구현체 {len({d.cls for d in hits})}개 중]"]
            # ponytail: name-based fallback tiers when the type route finds nothing: declared type (Foo / FooImpl) >
            # same file > every same-named def. Add real type inference if false edges still show up.
            # A typed Java receiver with no project body (library type, Spring Data repository interface) goes
            # nowhere: falling back would tie repo.findAll() to the caller's own findAll().
            typed = f.lang == "java" and t
            for d in hits or [d for d in cands if t and d.cls in (t, t + "Impl")] or \
                    ([] if typed else [d for d in cands if d.file == f.file] or cands):
                edges.append((pos, d, conds + mark, rel(site)))
                if d is not f:
                    called.add(d.id)
        for pos, verb, base, path, conds, site in f.http:
            # service-url's own path (e.g. /eqps) may be the target's context-path or part of its mapping
            want = (path, urlparse(base).path + path)
            for d, everb, epath in endpoints:  # HTTP targets stay out of `called`: controllers remain entries
                if (verb is None or everb is None or verb == everb) and any(_same(w, epath) for w in want):
                    edges.append((pos, d, conds + [f"{verb or 'HTTP'} {base}{path}"], rel(site)))
                    d.via_http |= folder(d.file) != folder(f.file)
        f.children, seen = [], set()
        for pos, d, conds, site in sorted(edges, key=lambda e: e[0]):
            if (k := (d.id, tuple(conds), pos)) not in seen:  # one row per call site
                seen.add(k)
                f.children.append((d.id, conds, site))

    def deep(f):  # its tree shows 2+ rows under it: one row (a leaf or a self-call ↻) is not worth listing
        return len(f.children) > 1 or any(c != f.id and funcs[c].children for c, *_ in f.children)

    # shallow (<= 2-row tree) and *RPL.java funcs are never listed as entries; they still show up inside trees
    entries = [f for f in funcs.values() if f.id not in called and deep(f) and not f.file.endswith("RPL.java")]
    return funcs, entries


if __name__ == "__main__":
    funcs, entries = analyze({
        "a.py": b"from b import helper\ndef main(x):\n    if x > 0:\n        helper(x)\n    elif x < -5:\n"
                b"        pass\n    else:\n        Svc(x).run()\nclass Svc:\n    def __init__(self, v): self.v = v\n"
                b"    def run(self): return self.v\nif __name__ == '__main__':\n    main(1)\n",
        "b.py": b"def helper(y):\n    for i in range(y):\n        helper2(i)\ndef helper2(z):\n    helper2(z)\n"
                b"def unused(): pass\ndef rec(n): rec(n)\n",
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
        "a.py:<module>", "A.java:A.main", "w.ts:go", "x.c:f", "EqpService.java:EqpService.get"}, \
        sorted(f"{e.file}:{e.name}" for e in entries)
    lab = lambda conds: [c if isinstance(c, str) else c[0] for c in conds]  # branch (label, line) -> label
    tree = {f"{f.file}:{f.name}": [(funcs[c].name, lab(cs)) for c, cs, _ in f.children] for f in funcs.values()}
    assert tree["a.py:<module>"] == [("main", ["if __name__ == '__main__'"])]
    assert tree["a.py:main"] == [("helper", ["if x > 0"]), ("Svc.run", ["else"]),
                                 ("Svc.__init__", ["else"])], tree["a.py:main"]
    assert [cs for _, cs, _ in funcs["a.py:2:main"].children] == [[("if x > 0", 3)], [("else", 7)], [("else", 7)]]
    assert tree["b.py:helper"] == [("helper2", [])]
    assert tree["A.java:A.main"] == [("A.run", ["if (a.length > 0)"]), ("A.run", ["else if (a.length < 0)"]),
                                     ("A.run", ["else"]), ("A.A", [])], tree["A.java:A.main"]
    assert tree["A.java:A.run"] == [("A.done", ["switch (n)", "case 1"])], tree["A.java:A.run"]
    assert tree["A.java:A.done"] == [("A.done", ["catch (Exception e)"])], tree["A.java:A.done"]
    assert tree["w.ts:go"] == [("step", ["if n > 1"]), ("stop", ["else"]), ("step", [])], tree["w.ts:go"]
    assert tree["m.go:S.Run"] == [("work", ["if a > 0"])]
    assert tree["x.c:f"] == [("g", ["if (a)"]), ("g", ["else"])], tree["x.c:f"]
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
    site = {f"{f.file}:{f.name}": [s for *_, s in f.children] for f in funcs.values()}  # (file line, call code)
    assert site["EqpService.java:EqpService.get"] == [
        (6, "eqpsClient.find(id)"), (7, 'restTemplate.getForObject(eqpsUrl + EQP_API + "/" + id, Eqp.class)')]
    assert site["EqpsClient.java:EqpsClient.find"] == [None]  # Feign: the HTTP hop has no call site of its own
    assert site["EqpService.java:EqpService.ping"] == [(10, 'eqpsUrl + "/health"')], site["EqpService.java:EqpService.ping"]
    assert not any(f.via_http for f in funcs.values())  # all in one folder: HTTP targets stay first entries
    anno = {f.name: f.anno for f in funcs.values()}
    assert (anno["EqpController.find"], anno["EqpController.list"], anno["EqpService.get"]) == (
        '@GetMapping("/{id}")', '@RequestMapping(value = {"/list", "/all"}, method = RequestMethod.GET)', ""), anno
    funcs, entries = analyze({  # two mapped service folders: the HTTP-called controller is no first entry point
        "caller/src/S.java": b'class S { @Value("${u}") String u; void a() { rt.getForObject(u + "/x", String.class); } }',
        "eqps/src/C.java": b'@RestController class C {\n @GetMapping("/x") String x() {\n y();\n z(); y(); return ""; }'
                           b' void y() {} void z() {} }',
        "eqps/src/EqpRPL.java": b"class EqpRPL { void handle() { done(); } void done() {} }",  # uncalled, but RPL
    })
    assert {e.name for e in entries} == {"S.a", "C.x"} and [e.name for e in entries if not e.via_http] == ["S.a"]
    assert [funcs[c].name for c, *_ in funcs["eqps/src/EqpRPL.java:1:handle"].children] == ["EqpRPL.done"]
    assert [(funcs[c].name, s) for c, _, s in funcs["eqps/src/C.java:2:x"].children] == [  # y twice: a row per call
        ("C.y", (3, "y()")), ("C.z", (4, "z()")), ("C.y", (4, "y()"))]
    funcs, _ = analyze({  # Spring DI: interface -> bean implementation (Qualifier > Primary > field name)
        "pay/PayService.java": b"public interface PayService { void pay(); }",
        "pay/CardPay.java": b'@Service("cardPay") public class CardPay implements PayService { public void pay() { c(); } '
                            b'void c() {} }',
        "pay/BankPay.java": b"@Service @Primary public class BankPay implements PayService { private final PayRepo repo; "
                            b"public void pay() { repo.pay(); } }",
        "pay/PayRepo.java": b"public interface PayRepo extends JpaRepository<Pay, Long> { void pay(); }",  # no body
        "pay/AbstractPay.java": b"public abstract class AbstractPay implements PayService { public void pay() {} }",
        "pay/KakaoPay.java": b"@Service public class KakaoPay extends AbstractPay { void go() { pay(); } }",
        "pay/NaverPay.java": b"public class NaverPay implements PayService { public void pay() {} }",  # bean via @Bean
        "pay/PayConfig.java": b"@Configuration class PayConfig { @Bean PayService naverPay() { return new NaverPay(); } }",
        "pay/FakePay.java": b"public class FakePay implements PayService { public void pay() {} }",  # not a bean
        "pay/Rule.java": b"interface Rule { void check(); } @Component class RuleA implements Rule { public void check() {} }"
                         b" @Component class RuleB implements Rule { public void check() {} }",
        "pay/PayController.java": b"""@RestController class PayController {
            private final PayService payService;
            @Qualifier("cardPay") private final PayService card;
            private final KakaoPay kakao;
            private final Rule rule;
            private final Rule ruleB;
            PayController(@Qualifier("naverPay") PayService naver) { this.naver = naver; }
            void a() { payService.pay(); card.pay(); naver.pay(); kakao.pay(); rule.check(); ruleB.check(); }
        }""",
    })
    kids = lambda fid: [(funcs[c].name, lab(cs)) for c, cs, _ in funcs[fid].children]
    assert kids("pay/PayController.java:8:a") == [
        ("BankPay.pay", []), ("CardPay.pay", []), ("NaverPay.pay", []), ("AbstractPay.pay", []),  # Primary/Qualifier/
        ("RuleA.check", ["[구현체 2개 중]"]), ("RuleB.check", ["[구현체 2개 중]"]), ("RuleB.check", [])], \
        kids("pay/PayController.java:8:a")  # @Bean name / inherited / ambiguous -> all, marked / field name
    assert kids("pay/KakaoPay.java:1:go") == [("AbstractPay.pay", [])]  # inherited, not every pay() around
    assert kids("pay/BankPay.java:1:pay") == []  # repo.pay(): repository interface, not BankPay.pay itself
    cond = "x > 1 && x < 100 && x != 5 && x != 7 && x != 9 && x != 11 && x != 13 && x != 15 && x != 17"
    params = "(String alpha, String beta, String gamma, String delta, " * 4 + "String omega)"
    funcs, _ = analyze({"L.java": f"class L {{ void a{params} {{ if ({cond}) b(); }} void b() {{}} }}".encode()})
    assert funcs["L.java:1:a"].children[0][1] == [(f"if ({cond})", 1)] and funcs["L.java:1:a"].params == params
    funcs, _ = analyze({  # Python UI -> Spring API over HTTP; the UI's base URL is a wildcard
        "ui/api.py": b'''import requests
session = requests.Session()
def api_get(path):
    return requests.get(BASE_URL + path)
def load_users():
    return api_get("/api/v1/users")
def load_user(uid):
    return session.get(f"{BASE_URL}/api/v1/users/{uid}", timeout=3)
def remove(uid):
    call_api("DELETE", "{}/api/v1/users/{}".format(BASE_URL, uid))
def call_api(method, url):
    return requests.request(method, url)
def search(q):
    return requests.post(BASE_URL + "/api/v1/users/search?q=%s" % q)
def one():
    pass
''',
        "api/UserController.java": b'''@RestController @RequestMapping("/api/v1/users") class UserController {
    @GetMapping public List<User> getUsers() { return null; }
    @GetMapping("/{id}") public User getUser(@PathVariable String id) { return svc.one(id); }
    @DeleteMapping("/{id}") public void delete(@PathVariable String id) { }
    @PostMapping("/search") public List<User> search(String q) { return null; }
}''',
    })
    py = {f.name: [(funcs[c].name, lab(cs)) for c, cs, _ in f.children] for f in funcs.values() if f.lang == "python"}
    assert py["api_get"] == py["call_api"] == []  # requests.* is the hop itself; its url is only a variable here
    assert py["load_users"] == [("api_get", []), ("UserController.getUsers", ["GET /api/v1/users"])], py
    assert py["load_user"] == [("UserController.getUser", ["GET /api/v1/users/{}"])], py
    assert py["remove"] == [("call_api", []), ("UserController.delete", ["DELETE /api/v1/users/{}"])], py
    assert py["search"] == [("UserController.search", ["POST /api/v1/users/search"])], py
    assert funcs["api/UserController.java:3:getUser"].children == []  # svc.one(): never Python's one()
    assert funcs["api/UserController.java:3:getUser"].via_http  # reached from another folder over HTTP
    assert (_py_path("http://h:80/ctx/x?a=1"), _py_path("{}"), _py_path("{}users")) == ("/ctx/x", None, None)
    assert (folder("caller/src/S.java"), folder("S.java"), keep("a/src/test/T.java"), keep("a/src/T.java")) == \
        ("caller", "", False, True)
    print("ok")
