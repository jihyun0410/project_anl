import streamlit as st

from analyzer import CONFIG, LANGS, analyze, folder, keep
from treeview import build, render

MAX_NODES = 300  # inline tree cap; bigger trees open in the popup instead
FULL_NODES = 5000  # ponytail: popup cap, cycles/fan-out can blow a tree up to millions of rows; add lazy expand if hit

CSS = """
.ct { overflow-x: auto; padding-bottom: 8px; color: var(--st-text-color);
      font: 13px/1.7 Consolas, "D2Coding", monospace; }
.ct-r { white-space: pre; width: max-content; cursor: pointer; border-radius: 3px; }
.ct-r:hover { background: rgba(106, 174, 232, 0.2); }
.ct-p, .ct-s { color: #8a99ab; }
.ct-c { color: #e03131; font-weight: 700; }
.ct-h { color: #1c7ed6; font-weight: 700; }
.ct-full { max-height: 65vh; overflow: auto; }
"""
JS = """
export default function ({ data, parentElement, setTriggerValue }) {
  let box = parentElement.querySelector(".ct");
  if (!box) {
    box = document.createElement("div");
    box.className = "ct";
    parentElement.appendChild(box);
  }
  box.innerHTML = data;
  box.querySelectorAll("[data-id]").forEach((g) => {
    g.onclick = () => setTriggerValue("clicked", g.dataset.id);
  });
}
"""
TREE = st.components.v2.component("call_tree", css=CSS, js=JS)

st.set_page_config(page_title="소스 진입점 분석기", layout="wide")
st.title("소스 진입점 분석기")
ss = st.session_state
ss.setdefault("maps", [])   # [set of top-level folders] analysed together
ss.setdefault("cache", {})  # frozenset(folders) -> analyze() result
ss.setdefault("sel", None)  # ("dir", folder) | ("file", path)


def unique(files):  # plain file uploads may share a name across services: Foo.java, Foo(2).java
    out = {}
    for f in files:
        if not keep(f.name):
            continue
        stem, dot, ext = f.name.rpartition(".")
        name, i = f.name, 2
        while name in out:
            name, i = f"{stem}({i}){dot}{ext}", i + 1
        out[name] = f.getvalue()
    return out


def label(d):
    return d or "(개별 파일)"


def group(d):
    return next((m for m in ss.maps if d in m), {d})


def result(g):
    if (k := frozenset(g)) not in ss.cache:
        with st.spinner("분석 중..."):
            ss.cache[k] = analyze({p: b for p, b in ss.files.items() if folder(p) in g})
    return ss.cache[k]


def select(s):
    ss.sel = s


@st.dialog("폴더 Mapping")
def mapping(folders):
    picked = st.multiselect("함께 분석할 폴더를 선택하세요", folders, format_func=label)
    if st.button("완료", type="primary", disabled=len(picked) < 2):
        g = set(picked)  # a folder belongs to one mapping: pull it out of older ones
        ss.maps = [m - g for m in ss.maps if len(m - g) > 1] + [g]
        st.rerun()


def detail(f):
    st.markdown(f"`{f.name}{f.params}`  \n{f.file} : {f.line}행")
    st.code(f.code, language=f.lang, line_numbers=True)


show = st.dialog("메서드 상세", width="large")(detail)


@st.dialog("전체 트리", width="large")
def full(funcs, fid):
    root, cut = build(funcs, fid, FULL_NODES)
    clicked = TREE(data=f'<div class="ct-full">{render(root)}</div>', key=f"full:{fid}",
                   on_clicked_change=lambda: None).clicked
    if cut:
        st.caption(f"노드가 너무 많아 {FULL_NODES}개까지만 표시했습니다.")
    if clicked:  # one dialog at a time: the code opens below the tree, not in a second popup
        detail(funcs[clicked])


def opened(funcs, fid):  # too big to draw inline: open the popup instead and leave the toggle off
    if ss[f"open:{fid}"] and build(funcs, fid, MAX_NODES)[1]:
        ss[f"open:{fid}"], ss.popup = False, fid


_, mid, _ = st.columns([1, 2, 1])
with mid:
    by_folder = st.radio("업로드 단위", ["폴더", "파일"], horizontal=True) == "폴더"
    files = st.file_uploader("프로젝트 폴더를 선택하세요 (다시 선택하면 폴더가 추가됩니다)" if by_folder else
                             "소스 파일과 application.yml을 끌어다 놓거나 선택하세요 (여러 개 가능)",
                             type=sorted(LANGS.keys() | CONFIG),
                             accept_multiple_files="directory" if by_folder else True)
    if st.button("분석", type="primary", disabled=not files):
        ss.files = unique(files)
        names = {folder(p) for p in ss.files}
        ss.maps = [m for m in ss.maps if m <= names]
        ss.cache, ss.sel = {}, None

if ss.get("files"):
    folders = sorted({folder(p) for p in ss.files})
    with st.sidebar:
        if st.button("Mapping하기", disabled=len(folders) < 2):
            mapping(folders)
        for i, m in enumerate(ss.maps):
            st.button(f"🔗 {' + '.join(sorted(map(label, m)))}  ✕", key=f"unmap:{i}", type="tertiary",
                      help="눌러서 Mapping 해제", on_click=ss.maps.remove, args=(m,))
        for d in folders:
            mapped = len(group(d)) > 1
            with st.expander(f"📁 {label(d)}" + (" 🔗" if mapped else "")):
                st.button("Mapping된 폴더 전체 진입점" if mapped else "폴더 전체 진입점", key=f"dir:{d}",
                          type="primary" if ss.sel == ("dir", d) else "secondary", on_click=select, args=(("dir", d),))
                last = None
                for p in sorted(p for p in ss.files if folder(p) == d and p.rsplit(".", 1)[-1].lower() in LANGS):
                    sub = (p[len(d) + 1:] if d else p).rpartition("/")[0]
                    if sub and sub != last:
                        st.caption(sub)
                    last = sub
                    st.button(p.rsplit("/", 1)[-1], key=f"file:{p}", on_click=select, args=(("file", p),),
                              type="primary" if ss.sel == ("file", p) else "tertiary")

if ss.sel is None:
    if ss.get("files"):
        st.info("사이드바에서 폴더나 파일을 선택하세요.")
else:
    kind, x = ss.sel
    g = group(x if kind == "dir" else folder(x))
    funcs, entries = result(g)
    if kind == "dir":
        st.subheader("📁 " + " + ".join(sorted(map(label, g))))
        entries = [e for e in entries if not e.via_http]  # first entry points only; the rest hang off their trees
    else:
        st.subheader("📄 " + x)
        entries = [e for e in entries if e.file == x]
    q = st.text_input("진입점 검색", placeholder="메서드 이름 또는 파일 경로").lower()
    shown = [e for e in entries if q in f"{e.name} {e.file}".lower()]
    st.markdown(f"**진입점 {len(shown)}개**" + (f" / 전체 {len(entries)}개" if q else ""))
    for e in shown:
        sig = f"{e.anno} {e.name}{e.params}".strip().replace("`", "'")  # a stray backtick would break the code span
        if st.toggle(f"`{sig}` · {e.file}:{e.line}", key=f"open:{e.id}",  # only opened trees get drawn
                     on_change=opened, args=(funcs, e.id)):
            root, cut = build(funcs, e.id, MAX_NODES)
            clicked = TREE(data=render(root), key=f"tree:{e.id}", on_clicked_change=lambda: None).clicked
            if cut:
                st.caption(f"노드가 많아 {MAX_NODES}개까지만 표시했습니다.")
            if clicked:
                show(funcs[clicked])
    if p := ss.pop("popup", None):  # set by opened(); popped so a later rerun doesn't reopen it
        full(funcs, p)
