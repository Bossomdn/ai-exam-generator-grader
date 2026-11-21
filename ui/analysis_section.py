"""แท็บ 4 ขั้นที่ 5: วิเคราะห์ข้อสอบจากผลสอบจริง → Item Analyst วินิจฉัยข้อที่ผิดปกติ
→ ปรับปรุงข้อสอบผ่านลูป Question Generator ⇄ Quality Reviewer → ครูเลือกใช้ฉบับปรับปรุง"""

import dataclasses
import time

import streamlit as st

from src import item_analysis, item_analyst, planner, school_db, scoring, sources
from src.generation_graph import GenerationResult, run_revision
from src.item_analysis import ExamStats
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, AgentStep, Exam, ItemAnalystReport, Question, StudentRecord

from .common import set_exam, show_progress, state

ACTION_LABELS = {"keep": "✅ ใช้ต่อได้", "revise": "🔧 ปรับบางส่วน", "rewrite": "✏️ เขียนใหม่ทั้งข้อ"}
REVISED_SUFFIX = " (ฉบับปรับปรุงจากผลสอบ)"


def render(ready: bool) -> None:
    exam = state.class_session.exam
    students, scope = students_in_scope()
    stats = item_analysis.analyze(exam, students)
    if stats is None:
        st.info(f"ต้องตรวจคะแนนอย่างน้อย {item_analysis.MIN_STUDENTS} คนก่อน จึงจะวิเคราะห์ข้อสอบได้")
        return
    show_stats(stats)
    key = (state.class_sitting.id, scope, stats.n, stats.mean_percent, stats.sd)
    analyst_section(exam, stats, key, ready)


def students_in_scope() -> tuple[list[StudentRecord], str]:
    """ผู้สอบห้องนี้ หรือทุกห้องที่ใช้ข้อสอบชุดเดียวกัน (มากคนขึ้น ค่าสถิติน่าเชื่อถือขึ้น)"""
    sitting = state.class_sitting
    with school_db.connect() as conn:
        others = [s for s in school_db.same_exam_sittings(conn, sitting.id) if s.id != sitting.id]
        if not others or not st.checkbox(
            f"รวมผลทุกห้องที่สอบข้อสอบชุดนี้ (+{', '.join(s.classroom.name for s in others)})",
            key="analysis_all_rooms",
        ):
            return state.class_session.students, sitting.classroom.name
        students = list(state.class_session.students)
        for other in others:
            students += school_db.load_session(conn, other.id)[1].students
    return students, "ทุกห้อง"


def show_stats(stats: ExamStats) -> None:
    reliability = (f"{stats.reliability:.2f} ({item_analysis.reliability_label(stats.reliability)})"
                   if stats.reliability is not None else "คำนวณไม่ได้")
    st.markdown(f"ผู้สอบที่ตรวจแล้ว **{stats.n} คน** · คะแนนเฉลี่ย {stats.mean_percent:g}% · SD {stats.sd:g} · "
                f"ค่าความเชื่อมั่น ({stats.reliability_name}) **{reliability}**")
    for warning in stats.warnings:
        st.caption(f"⚠️ {warning}")
    st.dataframe([{
        "ข้อ": i.question_id, "ชนิด": TYPE_LABELS[i.type], "หัวข้อ": i.concept,
        "p": i.p, "ความยาก": i.p_label, "r": i.r, "อำนาจจำแนก": i.r_label,
        "ข้อสังเกต": " / ".join(i.flags) or "ใช้ได้",
    } for i in stats.items], hide_index=True)
    st.caption(f"p = ความยาก (0.20–0.80 ใช้ได้) · r = อำนาจจำแนก (ตั้งแต่ 0.20 ใช้ได้) · "
               f"กลุ่มเก่ง/กลุ่มอ่อนกลุ่มละ {stats.group_size} คน (27% ตามคะแนนรวม) · ค่าความเชื่อมั่นตั้งแต่ 0.70 ใช้ได้")
    with st.expander("ตัวเลือกที่นักเรียนเลือก (ปรนัย)"):
        for item in stats.items:
            if item.choices:
                st.markdown(f"**ข้อ {item.question_id}**")
                st.dataframe([{"ตัวเลือก": c.label + (" ✓ เฉลย" if c.is_key else ""), "ทั้งหมด": c.total,
                               "กลุ่มเก่ง": c.upper, "กลุ่มอ่อน": c.lower} for c in item.choices], hide_index=True)


# ---------------- Item Analyst ----------------

def analyst_section(exam: Exam, stats: ExamStats, key: tuple, ready: bool) -> None:
    if not stats.flagged:
        st.success("ไม่พบข้อที่ค่าสถิติผิดปกติ ข้อสอบชุดนี้ใช้ต่อได้")
        return
    if st.button(f"🔍 ให้ Item Analyst วิเคราะห์ข้อที่ควรตรวจ ({len(stats.flagged)} ข้อ)", disabled=not ready):
        started = time.perf_counter()
        with st.spinner("Item Analyst กำลังวิเคราะห์จากผลสอบจริง..."):
            try:
                report = item_analyst.analyze_items(exam, stats)
            except LLMError as e:
                st.error(str(e))
                return
        state.item_report = {"key": key, "report": report, "seconds": round(time.perf_counter() - started, 1)}
        state.item_revision = None
    saved = state.item_report
    if saved is None or saved["key"] != key:  # ผลเก่าวิเคราะห์จากข้อมูลคนละชุด
        return
    show_report(exam, saved["report"])
    revise_section(exam, stats, saved, ready)


def show_report(exam: Exam, report: ItemAnalystReport) -> None:
    st.info(f"**Item Analyst:** {report.summary}")
    for d in report.items:
        st.markdown(f"**ข้อ {d.question_id}** · {ACTION_LABELS[d.action]}  \n{d.diagnosis}"
                    + (f"  \n*แนวทางแก้:* {d.suggestion}" if d.suggestion else ""))


# ---------------- ปรับปรุงข้อสอบ ----------------

def revise_section(exam: Exam, stats: ExamStats, saved: dict, ready: bool) -> None:
    issues, suggestions = item_analyst.revision_request(saved["report"], stats)
    if not issues:
        st.caption("Item Analyst แนะนำให้ใช้ทุกข้อต่อ ไม่ต้องแก้")
        return
    # ครูเป็นคนตัดสินว่าจะแก้ข้อไหน ระบบเลือกข้อที่ Item Analyst แนะนำไว้ให้ก่อน
    chosen = st.multiselect(
        "ข้อที่จะให้ปรับปรุง (เอาข้อที่ไม่อยากแก้ออกได้)", sorted(issues), default=sorted(issues),
        format_func=lambda i: f"ข้อ {i}", key="item_revise_ids_" + "_".join(map(str, saved["key"])),
    )
    issues = {i: issues[i] for i in chosen}
    suggestions = {i: suggestions[i] for i in chosen if i in suggestions}
    label = f"🛠️ ปรับปรุงข้อสอบตามคำแนะนำ ({len(issues)} ข้อ) — Question Generator แก้ → Quality Reviewer ตรวจ"
    if st.button(label, disabled=not ready or not issues):
        # ใช้เนื้อหาเดิมถ้าข้อสอบนี้สร้างในรอบนี้ ไม่งั้นใช้ชื่อชุดและหัวข้อแทน (ข้อสอบที่เปิดจากฐานข้อมูล)
        source = state.source if state.exam == exam and state.source else sources.from_topic(
            f"{exam.title}: {', '.join(exam.concepts)}")
        step = AgentStep(agent="Item Analyst", seconds=saved["seconds"],
                         summary=f"วิเคราะห์จากผลสอบจริง {stats.n} คน: ควรแก้ {len(issues)} ข้อ")
        result = show_progress(
            "กำลังปรับปรุงข้อสอบ: Generator แก้ข้อที่มีปัญหา → Reviewer ตรวจ", "ปรับปรุงข้อสอบ",
            lambda on_step: run_revision(source, planner.spec_from_exam(exam), exam, issues, suggestions,
                                         prior_steps=[step], on_step=on_step),
        )
        if result:
            state.item_revision = {"key": saved["key"], "result": result, "source": source, "ids": sorted(issues)}
    revision = state.item_revision
    if revision and revision["key"] == saved["key"]:
        show_revision(exam, revision["result"], revision["ids"])
        if st.button("ใช้ข้อสอบฉบับปรับปรุง (ดูเฉลย / พิมพ์ที่แท็บ 1)", type="primary"):
            use_revision(revision)


def question_text(q: Question) -> str:
    text = q.question
    if q.type in OBJECTIVE_TYPES:
        text += "\n" + "\n".join(f"- {scoring.choice_label(q, i)}" + (" ✓" if i == q.correct_choice else "")
                                 for i in range(len(q.choices)))
    elif q.model_answer:
        text += f"\n\n*คำตอบตัวอย่าง:* {q.model_answer}"
    return text


def show_revision(exam: Exam, result: GenerationResult, ids: list[int]) -> None:
    before = {q.id: q for q in exam.questions}
    after = {q.id: q for q in result.exam.questions}
    quality = {q.question_id: q for q in result.quality}
    st.markdown("**ข้อสอบฉบับปรับปรุง** (เทียบก่อน / หลังแก้)")
    for i in ids:
        q = quality.get(i)
        if q is None or not q.revisions:
            st.markdown(f"**ข้อ {i}** · ⚠️ Generator แก้ไม่สำเร็จ ยังเป็นข้อเดิม")
            continue
        status = {"fixed": "✅ Reviewer ตรวจผ่าน", "unchecked": "❔ Reviewer ไม่ได้ตรวจ"}.get(
            q.status, "⚠️ ยังมีปัญหา: " + " / ".join(q.issues))
        st.markdown(f"**ข้อ {i}** · {status}")
        col_before, col_after = st.columns(2)
        col_before.caption("ก่อนแก้")
        col_before.markdown(question_text(before[i]))
        col_after.caption("หลังแก้")
        col_after.markdown(question_text(after[i]))


def use_revision(revision: dict) -> None:
    result: GenerationResult = revision["result"]
    title = result.exam.title if result.exam.title.endswith(REVISED_SUFFIX) else result.exam.title + REVISED_SUFFIX
    set_exam(dataclasses.replace(result, exam=result.exam.model_copy(update={"title": title})), revision["source"])
    state.notice = f"ใช้ “{title}” แล้ว ดูเฉลยหรือพิมพ์ได้ที่แท็บ 1 · ใช้สอบห้องถัดไปได้ที่แท็บ 4 (ปิดการสอบนี้แล้วเริ่มใหม่)"
    st.rerun()
