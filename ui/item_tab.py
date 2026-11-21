"""สถิติข้อสอบและร่างข้อปรับปรุงของการสอบที่เปิดอยู่"""

import streamlit as st

from src import item_analysis, school_db, scoring
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, Exam, QuestionQuality

from .common import state, use_exam
from .create_tab import choose_source

ACTIONS = {"keep": "คงไว้", "revise": "แก้ไข", "retire": "เปลี่ยนข้อ"}


def render(ready: bool) -> None:
    session = state.class_session
    exam = session.exam
    if not any(s.results for s in session.students) or not any(q.type in OBJECTIVE_TYPES for q in exam.questions):
        return
    with school_db.connect() as conn:
        rows = school_db.objective_scores(conn, state.class_sitting.id)
    analysis = item_analysis.analyze(exam, rows)
    if not analysis.items:
        return
    questions = {q.id: q for q in exam.questions}
    with st.expander("วิเคราะห์ข้อปรนัยจากผลสอบ"):
        reliability = "คำนวณไม่ได้" if analysis.kr20 is None else f"{analysis.kr20:.3f}"
        st.caption(f"ผู้สอบที่มีผลครบ {analysis.students} คน · KR-20 {reliability}")
        st.caption("ค่าความยากคือร้อยละที่ตอบถูก อำนาจจำแนกเทียบกับจำนวนข้ออื่นที่ตอบถูก "
                   "การเว้นคำตอบนับเป็นผิด ค่าอาจไม่นิ่งเมื่อมีผู้สอบน้อย")
        st.dataframe([
            {
                "ข้อ": item.question_id,
                "คำถาม": questions[item.question_id].question,
                "ตอบถูก (%)": round(100 * item.proportion_correct, 1),
                "อำนาจจำแนก": round(item.discrimination, 3) if item.discrimination is not None else None,
                "จำนวนตามตัวเลือก": " · ".join(
                    f"{scoring.choice_label(questions[item.question_id], i)} = {count}"
                    + (" (เฉลย)" if i == questions[item.question_id].correct_choice else "")
                    for i, count in enumerate(item.choice_counts)
                ),
                "เว้น": item.omitted,
            }
            for item in analysis.items
        ], hide_index=True)
        if state.class_item_work and state.class_item_work["analysis"] != analysis:
            state.class_item_work = None
        if st.button("ขอข้อเสนอแนะจาก Item Analyst", disabled=not ready):
            try:
                with st.spinner("กำลังวิเคราะห์ข้อสอบ..."):
                    recommendations = item_analysis.recommend(exam, analysis)
                state.class_item_work = {"analysis": analysis, "recommendations": recommendations, "draft": None}
            except LLMError as e:
                st.error(str(e))
        work = state.class_item_work
        if work is not None:
            show_recommendations(exam, ready, work)


def show_recommendations(exam: Exam, ready: bool, work: dict) -> None:
    if not work["recommendations"]:
        st.info("Item Analyst ไม่ได้ส่งข้อเสนอแนะสำหรับข้อในชุดนี้")
        return
    st.dataframe([
        {"ข้อ": r.question_id, "ข้อเสนอ": ACTIONS[r.action], "เหตุผล": r.reason, "แนวทาง": r.suggestion}
        for r in work["recommendations"]
    ], hide_index=True)
    options = {r.question_id: r for r in work["recommendations"] if r.action != "keep"}
    if options:
        selected = st.multiselect("ข้อที่จะสร้างฉบับปรับปรุง", list(options), key="item_revision_questions")
        source = state.source if state.exam == exam else None
        if selected and source is None:
            st.caption("เลือกเนื้อหาที่ใช้ออกข้อสอบชุดเดิม เพื่อให้ผู้ตรวจทานตรวจคำถามที่แก้ได้")
            source = choose_source(key_prefix="item")
        if st.button("สร้างร่างฉบับปรับปรุง", disabled=not ready or not selected or source is None):
            try:
                with st.spinner("กำลังปรับข้อสอบและตรวจคุณภาพข้อที่แก้..."):
                    draft, review = item_analysis.revise(exam, source, [options[i] for i in selected])
                if draft == exam:
                    st.warning("โมเดลยังไม่ได้ส่งข้อที่แก้กลับมา ลองอีกครั้ง")
                else:
                    work["draft"] = (draft, review, source)
            except LLMError as e:
                st.error(str(e))
    if work["draft"] is None:
        return
    draft, review, draft_source = work["draft"]
    changed = [q for before, q in zip(exam.questions, draft.questions) if before != q]
    for q in changed:
        st.markdown(f"**ข้อ {q.id}**")
        st.text(q.question)
        st.text("\n".join(scoring.choice_label(q, i) for i in range(len(q.choices))))
        st.caption(f"เฉลย {scoring.choice_label(q, q.correct_choice)}")
        if q.id not in review.issues:
            st.warning("Reviewer ยังไม่ได้ตรวจข้อนี้")
        elif review.issues[q.id]:
            st.warning("\n".join(review.issues[q.id]))
    if state.exam == draft:
        st.success("เปิดร่างในแท็บสร้างข้อสอบแล้ว ใช้ร่างนี้สร้างการสอบครั้งใหม่ได้")
    elif st.button("เปิดร่างในแท็บสร้างข้อสอบ"):
        use_exam(draft, draft_source)
        quality = {}
        for q in changed:
            issues = review.issues.get(q.id)
            status = "unchecked" if issues is None else ("flagged" if issues else "fixed")
            quality[q.id] = QuestionQuality(question_id=q.id, status=status, issues=issues or [], revisions=1)
        state.quality = quality
        st.rerun()
