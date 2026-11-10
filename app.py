import streamlit as st

from analyzer import LANGS, analyze

MAX_ROWS = 300  # ponytail: per-tree row cap keeps huge call graphs renderable; add lazy expand if it gets hit

st.set_page_config(page_title="소스 진입점 분석기")
st.title("소스 진입점 분석기")

files = st.file_uploader("소스 파일을 끌어다 놓거나 선택하세요 (여러 개 가능)", type=sorted(LANGS),
                         accept_multiple_files=True)
if st.button("분석", type="primary", disabled=not files):
    st.session_state.result = analyze({f.name: f.getvalue() for f in files})


@st.dialog("메서드 상세", width="large")
def show(f):
    st.markdown(f"`{f.name}{f.params}`  \n{f.file} : {f.line}행")
    st.code(f.code, language=f.lang, line_numbers=True)


def sig(f):
    return "`" + f"{f.name}{f.params}".replace("`", "'") + "`"  # a stray backtick would break the code span


def tree(funcs, fid, prefix, path, key, budget):
    kids = funcs[fid].children
    for i, (cid, conds) in enumerate(kids):
        if budget[0] <= 0:
            st.caption(f"{prefix}… 최대 {MAX_ROWS}줄까지만 표시")
            return
        budget[0] -= 1
        c, last, k = funcs[cid], i == len(kids) - 1, f"{key}/{i}"
        cond = f"`{' › '.join(conds)}` " if conds else ""
        loop = " ↻" if cid in path else ""
        if st.button(f"{prefix}{'└─' if last else '├─'} {cond}{sig(c)} · {c.file}:{c.line}{loop}", key=k,
                     type="tertiary"):
            show(c)
        if not loop:
            tree(funcs, cid, prefix + (" " * 4 if last else "│   "), path | {cid}, k, budget)


if res := st.session_state.get("result"):
    funcs, entries = res
    st.subheader(f"진입점 {len(entries)}개")
    for e in entries:
        with st.expander(f"{sig(e)} · {e.file}:{e.line}"):
            if st.button(f"{sig(e)} 코드 보기", key=f"{e.id}/self", type="tertiary"):
                show(e)
            if not e.children:
                st.caption("호출하는 내부 메서드 없음")
            tree(funcs, e.id, "", {e.id}, e.id, [MAX_ROWS])
