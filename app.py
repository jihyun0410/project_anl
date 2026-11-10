import streamlit as st

from analyzer import CONFIG, LANGS, analyze
from treeview import build, render

MAX_NODES = 300  # ponytail: per-tree node cap keeps huge call graphs renderable; add lazy expand if it gets hit

CSS = """
.ct { overflow-x: auto; padding-bottom: 8px; }
.ct-n { cursor: pointer; }
.ct-n rect { fill: #eaf4fd; stroke: #6aaee8; stroke-width: 2; }
.ct-n:hover rect { fill: #d3e8fb; }
.ct-n text { fill: #2d3748; font: 12px Consolas, "D2Coding", monospace; text-anchor: middle; }
.ct-n .ct-t { font-weight: 700; }
.ct-n .ct-s { fill: #5a6b80; }
.ct-e { fill: none; stroke: #6aaee8; stroke-width: 2; }
.ct-a { fill: #6aaee8; }
.ct-l { font: 11px Consolas, monospace; text-anchor: middle; fill: var(--st-text-color);
        paint-order: stroke; stroke: var(--st-background-color); stroke-width: 4px; }
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


def unique(files):  # same file name from different services: Foo.java, Foo(2).java
    out = {}
    for f in files:
        stem, dot, ext = f.name.rpartition(".")
        name, i = f.name, 2
        while name in out:
            name, i = f"{stem}({i}){dot}{ext}", i + 1
        out[name] = f.getvalue()
    return out


_, mid, _ = st.columns([1, 2, 1])
with mid:
    files = st.file_uploader("소스 파일과 application.yml을 끌어다 놓거나 선택하세요 (여러 개 가능)",
                             type=sorted(LANGS.keys() | CONFIG), accept_multiple_files=True)
    if st.button("분석", type="primary", disabled=not files):
        st.session_state.result = analyze(unique(files))


@st.dialog("메서드 상세", width="large")
def show(f):
    st.markdown(f"`{f.name}{f.params}`  \n{f.file} : {f.line}행")
    st.code(f.code, language=f.lang, line_numbers=True)


if res := st.session_state.get("result"):
    funcs, entries = res
    st.subheader(f"진입점 {len(entries)}개")
    for i, e in enumerate(entries):
        label = f"{e.name}{e.params}".replace("`", "'")  # a stray backtick would break the code span
        with st.expander(f"`{label}` · {e.file}:{e.line}"):
            root, cut = build(funcs, e.id, MAX_NODES)
            clicked = TREE(data=render(root, f"ct{i}"), key=f"tree:{e.id}", on_clicked_change=lambda: None).clicked
            if cut:
                st.caption(f"노드가 많아 {MAX_NODES}개까지만 표시했습니다.")
            if clicked:
                show(funcs[clicked])
