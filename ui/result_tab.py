"""หน้าผลและคำแนะนำ: คะแนน, คะแนนรายหัวข้อ, คำแนะนำจาก Learning Advisor, แบบฝึกเพิ่ม, รายละเอียดรายข้อ, รายงาน PDF"""

import json

import streamlit as st

from src import config, export, scoring
from src.schemas import OBJECTIVE_TYPES, ConceptMastery, Exam, LearningReport, QuestionResult

from . import nav
from .common import run_with_progress, set_exam, show_panel, state


def render(ready: bool) -> None:
    exam, results, mastery, report = state.exam, state.results, state.mastery, state.report
    if not results:
        st.info("ยังไม่มีผลสอบ — ทำข้อสอบแล้วส่งตรวจก่อน")
        nav.link(nav.PRACTICE, "ไปทำข้อสอบ")
        return
    show_scores(results, mastery)
    if report:
        show_report(report, mastery, ready)
    else:
        st.warning("ยังไม่มีรายงานคำแนะนำ (การวิเคราะห์อาจล้มเหลว) ลองส่งตรวจใหม่อีกครั้ง")
    show_question_details(exam, results)
    show_downloads(exam, results, mastery, report)


def show_scores(results: list[QuestionResult], mastery: list[ConceptMastery]) -> None:
    t = scoring.totals(results)
    pct = 100 * t["score"] / t["max"] if t["max"] else 0
    c1, c2, c3 = st.columns(3)
    c1.metric("คะแนนรวม", f"{t['score']:g} / {t['max']:g} ({pct:.0f}%)")
    c2.metric("ตอนปรนัย", f"{t['objective_score']:g} / {t['objective_max']:g}" if t["objective_max"] else "–")
    c3.metric("ตอนอัตนัย", f"{t['subjective_score']:g} / {t['subjective_max']:g}" if t["subjective_max"] else "–")

    st.subheader("คะแนนรายหัวข้อ")
    st.caption(f"หัวข้อที่ต่ำกว่า {config.WEAK_THRESHOLD:g}% ถือว่าต้องเรียนเพิ่ม")
    for m in mastery:
        flag = "⚠️ ต้องเรียนเพิ่ม · " if m.percent < config.WEAK_THRESHOLD else "✓ "
        st.progress(
            min(m.percent / 100, 1.0),
            text=f"{flag}{m.concept} — {m.percent:.0f}% ({m.points_awarded:g}/{m.max_points:g}) · {', '.join(m.source_refs)}",
        )
    with st.expander("ดูเป็นตาราง"):
        st.dataframe(
            [{
                "หัวข้อ": m.concept,
                "คะแนน (%)": m.percent,
                "ได้": m.points_awarded,
                "เต็ม": m.max_points,
                "ข้อ": ", ".join(map(str, m.question_ids)),
                "ที่มา": ", ".join(m.source_refs),
            } for m in mastery],
            hide_index=True,
        )


def show_report(report: LearningReport, mastery: list[ConceptMastery], ready: bool) -> None:
    st.subheader("สรุปและคำแนะนำ")
    st.write(report.overall_summary)
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**จุดแข็ง**\n" + "\n".join(f"- {s}" for s in report.strengths))
    with col_b:
        st.markdown("**แผนการทบทวน**\n" + "\n".join(f"{i}. {export.strip_numbering(s)}" for i, s in enumerate(report.study_plan, 1)))

    st.markdown("#### เรื่องที่ต้องเรียนเพิ่ม / ปรับปรุง")
    pct_by_concept = {m.concept: m.percent for m in mastery}
    for w in report.weak_topics:
        with st.container(border=True):
            concept_pct = pct_by_concept.get(w.concept)
            st.markdown(f"**{w.concept}**" + (f" — {concept_pct:.0f}%" if concept_pct is not None else ""))
            st.write(w.why)
            if w.review:
                st.markdown("📖 **ควรทบทวน:** " + ", ".join(w.review))
            st.markdown("\n".join(f"- {a}" for a in w.actions))

    if report.answer_writing_tips:
        st.markdown("#### คำแนะนำการเขียนตอบอัตนัย\n" + "\n".join(f"- {tip}" for tip in report.answer_writing_tips))

    st.markdown("#### ฝึกต่อ")
    practice_prompt = st.text_area(
        "คำสั่งสำหรับแบบฝึกชุดถัดไป (แก้ได้)", value=report.practice_prompt, key=f"practice_{state.version}"
    )
    if st.button("สร้างแบบฝึกเพิ่มเรื่องที่อ่อน", disabled=state.source is None or not ready):
        # ใช้กราฟเดียวกับหน้าออกข้อสอบ แต่ให้ Request Planner วางสเปกเองโดยไม่ต้องรอยืนยัน
        result = run_with_progress(state.source, request=practice_prompt)
        if result:
            set_exam(result, state.source)
            state.notice = f"สร้างแบบฝึก “{result.exam.title}” แล้ว ทำได้เลย"
            st.switch_page(nav.PRACTICE)


def show_question_details(exam: Exam, results: list[QuestionResult]) -> None:
    st.subheader("รายละเอียดรายข้อ")
    questions = {q.id: q for q in exam.questions}
    for r in results:
        q = questions[r.question_id]
        if r.points_awarded >= r.max_points:
            icon = "✅"
        elif r.points_awarded == 0:
            icon = "❌"
        else:
            icon = "🟡"
        with st.expander(f"{icon} ข้อ {r.question_id} · {r.points_awarded:g}/{r.max_points:g} คะแนน · {r.concept}"):
            st.markdown(q.question)
            st.markdown(f"**คำตอบของคุณ:** {r.student_answer}")
            if q.type in OBJECTIVE_TYPES:
                st.markdown(f"**เฉลย:** {scoring.choice_label(q, q.correct_choice)}")
                st.caption(q.explanation)
                continue
            if r.criteria:
                st.dataframe(
                    [{
                        "เกณฑ์": c.criterion,
                        "คะแนน": f"{c.points_awarded:g}/{c.max_points:g}",
                        "หลักฐานจากคำตอบ": c.evidence,
                        "ความเห็น": c.comment,
                    } for c in r.criteria],
                    hide_index=True,
                )
            show_panel(r)
            if r.unverified_evidence:
                st.warning(
                    "ผู้ตรวจอ้างข้อความที่ไม่พบในคำตอบจริง ควรให้ผู้สอนตรวจซ้ำ: "
                    + " / ".join(f"“{e}”" for e in r.unverified_evidence)
                )
            st.markdown(f"**Feedback:** {r.feedback}")
            if r.missed_key_points:
                st.markdown("**ประเด็นที่ขาด:**\n" + "\n".join(f"- {p}" for p in r.missed_key_points))
            if r.misconceptions:
                st.markdown("**ความเข้าใจที่คลาดเคลื่อน:**\n" + "\n".join(f"- {p}" for p in r.misconceptions))
            st.markdown(f"**คำตอบตัวอย่าง:** {q.model_answer}")


def show_downloads(
    exam: Exam, results: list[QuestionResult], mastery: list[ConceptMastery], report: LearningReport | None
) -> None:
    st.markdown("#### พิมพ์รายงานผล")
    student_name = st.text_input("ชื่อผู้สอบ (แสดงในรายงาน ไม่บังคับ)", key="student_name")
    if st.button("📄 สร้างรายงานผล (PDF)"):
        with st.spinner("กำลังสร้าง PDF..."):
            try:
                state.files["report"] = export.report_pdf(exam, results, mastery, report, student_name)
            except export.ExportError as e:
                st.error(str(e))
    if "report" in state.files:
        st.download_button(
            "⬇️ รายงานผลการสอบ (PDF)", state.files["report"],
            file_name="report.pdf", mime="application/pdf",
        )
    st.download_button(
        "ดาวน์โหลดผลการประเมิน (JSON)",
        json.dumps({
            "exam": exam.model_dump(),
            "results": [r.model_dump() for r in results],
            "mastery": [m.model_dump() for m in mastery],
            "report": report.model_dump() if report else None,
        }, ensure_ascii=False, indent=2),
        file_name="assessment_report.json",
        mime="application/json",
    )
